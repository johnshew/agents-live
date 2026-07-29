#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML", "mcp[cli]<2", "jsonschema", "duckdb"]
# ///
"""Export-safe smoke tests for the agents-live package (§5.1 "exported
test suite", F4).

Unlike ``test_headless.py`` (life-coupled, export-excluded), every test
here runs against temp projects only and works in BOTH layouts: the flat
checkout (``uv run --script test_package_smoke.py``) and the installed
package (``python -m unittest tests.test_smoke`` in the exported repo,
where the assembler ships this file as ``tests/test_smoke.py``).
"""
from __future__ import annotations

import ast
import contextlib
import ctypes
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import queue
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

try:  # installed package layout
    from agents_live import (  # type: ignore
        activate, adminlog, agent_adapters, cli, completions, dashboards,
        headless, health_check, heartbeat, hidden, hostruntime, init, migrate,
        ownership, paths, plugins, preflight, doctor, repos, run, schedules,
        smoketest, spawn, status, uninstall, update_check, upgrade, triggers,
        watchpolicy, watchsource, winwatch, wintasks,
    )
    from agents_live.cli_spec import (
        Arg, Cmd, COMMANDS, GLOBAL_ARGS, HELP_ARG, POST_COMMAND_ARGS,
        render_docs_block, validation_error, visible_args,
    )
except ImportError:  # flat checkout layout
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import activate
    import adminlog
    import agent_adapters
    import cli
    import completions
    import dashboards
    import headless
    import health_check
    import heartbeat
    import hidden
    import hostruntime
    import init
    import migrate
    import ownership
    import paths
    import plugins
    import preflight
    import doctor
    import repos
    import run
    import schedules
    import smoketest
    import spawn
    import status
    import update_check
    import upgrade
    import uninstall
    import triggers
    import watchpolicy
    import watchsource
    import winwatch
    import wintasks
    from cli_spec import (
        Arg, Cmd, COMMANDS, GLOBAL_ARGS, HELP_ARG, POST_COMMAND_ARGS,
        render_docs_block, validation_error, visible_args,
    )


def schedule_lines(name: str) -> list[str]:
    """The crontab lines activation would install for *name*."""
    return triggers.render(headless.schedule_spec(name))


def watcher_reboot_line(name: str) -> str:
    """The @reboot respawn line activation would install for *name*."""
    return triggers.render(headless.watcher_spec(name))[0]


def cron_root(root: Path | str) -> str:
    """*root* as a crontab line spells it.

    A crontab line is shell text: the renderer writes paths through
    shlex.quote and the matchers read them back through shlex.split. A
    bare interpolation round-trips only while the path has no shell
    metacharacters - which a Windows root, full of backslashes, does
    not. Fixtures that hand-write lines quote them the same way.
    """
    return shlex.quote(str(root))


@contextlib.contextmanager
def _cwd_restored_before_cleanup(saved: Path):
    """Return to *saved* on the way out, ahead of any enclosing cleanup.

    A test that chdirs into a temporary directory has to leave it before
    the directory is removed: Windows refuses to delete a directory that
    is some process's current directory, so restoring the cwd in a later
    finally block is too late.
    """
    try:
        yield
    finally:
        os.chdir(saved)


def link_directory(link: Path, target: Path) -> None:
    """Point *link* at *target* with whatever alias the platform allows.

    Windows refuses directory symlinks to an unprivileged account, but
    lets anyone create a junction - and a junction is the reparse point
    a real checkout is likely to contain, so it is the alias worth
    testing there.
    """
    if sys.platform != "win32":
        link.symlink_to(target, target_is_directory=True)
        return
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if completed.returncode != 0:
        raise OSError(
            f"could not create a junction at {link}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}")


def unlink_directory(link: Path) -> None:
    """Remove an alias made by :func:`link_directory`.

    A junction is a directory entry and comes off with rmdir; a POSIX
    symlink to a directory is still a link and comes off with unlink.
    Neither touches what the alias pointed at.
    """
    if sys.platform == "win32":
        link.rmdir()
    else:
        link.unlink()


class _TempProject(unittest.TestCase):
    """A temp project selected via the env var, restored on stop."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".agents-live.toml").write_text("", encoding="utf-8")
        (self.root / "Agents" / "data").mkdir(parents=True)
        self._saved_env = os.environ.get(paths.ENV_VAR)
        os.environ[paths.ENV_VAR] = str(self.root)
        # Isolate user-level runtime state (logs, beacons, watch hashes)
        # so tests never touch the developer's real state home.
        self._saved_state_home = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(self.root / "xdg-state")
        self._saved_data_home = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = str(self.root / "xdg-data")
        self._saved_config_home = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "xdg-config")
        paths.clear_cache()
        # The crontab-shaped expectations in this suite predate Windows
        # support, and the Task Scheduler branch registers real tasks on
        # the developer's machine. Pin the dispatcher so every host runs
        # the same assertions; TestWindowsScheduling covers the other
        # branch with no host writes at all.
        scheduler = mock.patch.object(
            hostruntime, "native_scheduler", return_value=hostruntime.CRONTAB)
        scheduler.start()
        self.addCleanup(scheduler.stop)

    def tearDown(self) -> None:
        if self._saved_env is None:
            os.environ.pop(paths.ENV_VAR, None)
        else:
            os.environ[paths.ENV_VAR] = self._saved_env
        if self._saved_state_home is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self._saved_state_home
        if self._saved_data_home is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self._saved_data_home
        if self._saved_config_home is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._saved_config_home
        os.environ.pop(cli.INIT_REPO_ENV_VAR, None)
        paths.clear_cache()
        self._tmp.cleanup()

    def write_agent(self, name: str, body: str) -> None:
        agent_dir = self.root / ".claude" / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / f"{name}.md").write_text(body, encoding="utf-8")


TEST_CRON_SCHEDULE = "0 6 * * *"
AGENT_DEFINITION = f"""---
description: Smoke fixture. Never delegate to this agent.
disable-model-invocation: true
runtime: none
mode: plan
schedule: "{TEST_CRON_SCHEDULE}"
pre-processor: Agents/handlers/prep.py
---
Smoke fixture body.
"""
MULTI_TRIGGER_DEFINITION = AGENT_DEFINITION.replace(
    f'schedule: "{TEST_CRON_SCHEDULE}"',
    f'schedule: "{TEST_CRON_SCHEDULE}"\nwatchPath: Agents/data')
FOREIGN_REPO = "/tmp/foreign-agents-live-project"


class TestSmoketestDispatch(_TempProject):
    def test_cleanup_finds_runs_in_either_invocation_form(self) -> None:
        """Issue #193: the installed form carries no script path.

        The flat checkout dispatches ``run.py`` through ``uv run
        --script``; an installed package dispatches the pinned CLI shim
        with a ``run`` subcommand. Matching only the first meant cleanup
        silently found nothing for every user of the released package.
        """
        smoketest = importlib.import_module(
            f"{cli.__package__}.smoketest" if cli.__package__ else "smoketest")
        source_form = (
            "uv run --script /opt/agents_live/run.py --name _smoketest-cron")
        installed_form = (
            r"C:\tools\agents-live.exe --repo D:\project run "
            "--name _smoketest-cron --scheduled")
        self.assertTrue(smoketest._is_agent_run(source_form))
        self.assertTrue(smoketest._is_agent_run(installed_form))
        self.assertFalse(smoketest._is_agent_run(
            r"C:\tools\agents-live.exe --repo D:\project status"))

    def test_cleanup_removes_smoketest_watch_hashes(self) -> None:
        smoketest = importlib.import_module(
            f"{cli.__package__}.smoketest" if cli.__package__ else "smoketest")
        state_dir = paths.repo_state_dir(self.root)
        state_dir.mkdir(parents=True, exist_ok=True)
        for name in smoketest.SMOKETEST_AGENT_NAMES:
            (state_dir / f"{name}-watch-hashes.json").write_text(
                "{}", encoding="utf-8")
        unrelated = state_dir / "production-watch-hashes.json"
        unrelated.write_text("{}", encoding="utf-8")

        stopped = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            mock.patch.object(smoketest, "_stop_smoketest_processes",
                              return_value=[]),
            mock.patch.object(smoketest.subprocess, "run", return_value=stopped),
            mock.patch.object(smoketest, "_smoketest_run_pids", return_value=[]),
            mock.patch.object(smoketest.schedules, "is_active",
                              return_value=False),
            mock.patch.object(smoketest, "find_watcher_pid", return_value=None),
        ):
            residue, diagnostics = smoketest.cleanup()

        self.assertEqual(residue, [])
        self.assertEqual(diagnostics, [])
        for name in smoketest.SMOKETEST_AGENT_NAMES:
            self.assertFalse(
                (state_dir / f"{name}-watch-hashes.json").exists())
        self.assertTrue(unrelated.is_file())

    def test_changed_files_round_trip_uses_run_contract(self) -> None:
        smoketest = importlib.import_module(
            f"{cli.__package__}.smoketest" if cli.__package__ else "smoketest")
        completed = subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")
        with mock.patch.object(smoketest.subprocess, "run",
                               return_value=completed) as run:
            self.assertEqual(
                smoketest.run_agent("fixture", ["src/a.py", "src/b.py"]),
                "ok\n",
            )

        command = run.call_args.args[0]
        flag_index = command.index("--changed-files")
        self.assertEqual(
            json.loads(command[flag_index + 1]),
            ["src/a.py", "src/b.py"],
        )

    def test_watcher_log_read_starts_at_current_run_and_waits_for_done(
            self) -> None:
        smoketest = importlib.import_module(
            f"{cli.__package__}.smoketest" if cli.__package__ else "smoketest")
        log_path = headless.logs_root() / "_smoketest-watcher.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        historical = {
            "run_id": "old",
            "phase": "agent",
            "status": "ok",
            "output": '{"status":"fail"}',
        }
        done = {"run_id": "old", "phase": "done", "status": "ok"}
        log_path.write_text(
            f"{json.dumps(historical)}\n{json.dumps(done)}\n",
            encoding="utf-8",
        )
        current_run_offset = log_path.stat().st_size
        current_events = [
            {"run_id": "watch", "phase": "start", "trigger": "file-change"},
            {
                "run_id": "watch",
                "phase": "agent",
                "status": "ok",
                "output": '{"status":"pass"}',
            },
            {"run_id": "manual", "phase": "start", "trigger": "manual"},
            {"run_id": "manual", "phase": "done", "status": "ok"},
        ]
        with log_path.open("a", encoding="utf-8") as log_file:
            for event in current_events:
                log_file.write(f"{json.dumps(event)}\n")

        with self.assertRaises(smoketest.SmokeFailure):
            smoketest.read_agent_output_from_log(
                "_smoketest-watcher",
                start_offset=current_run_offset,
                require_done=True,
                required_trigger="file-change",
            )

        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(
                f"{json.dumps({'run_id': 'watch', 'phase': 'done', 'status': 'ok'})}\n")
        self.assertEqual(
            smoketest.read_agent_output_from_log(
                "_smoketest-watcher",
                start_offset=current_run_offset,
                require_done=True,
                required_trigger="file-change",
            ),
            '{"status":"pass"}',
        )


class TestPathsResolver(_TempProject):
    def test_env_var_pins_root(self) -> None:
        self.assertEqual(paths.resolve_root(), self.root)

    def test_marker_walkup_from_cwd(self) -> None:
        saved = Path.cwd()
        os.environ.pop(paths.ENV_VAR, None)
        paths.clear_cache()
        nested = self.root / "a" / "b"
        nested.mkdir(parents=True)
        try:
            os.chdir(nested)
            self.assertEqual(paths.resolve_root(), self.root)
        finally:
            os.chdir(saved)

    def test_plugin_declarations_validate_repo_relative_wheels_and_sha256(self) -> None:
        wheel = self.root / "Agents" / "plugins" / "example.whl"
        wheel.parent.mkdir(parents=True)
        wheel.write_bytes(b"wheel")
        (self.root / ".agents-live.toml").write_text(
            '[plugins]\nexample = { path = "Agents/plugins/example.whl", '
            f'sha256 = "{hashlib.sha256(b"wheel").hexdigest()}" }}\n',
            encoding="utf-8",
        )
        declaration = paths.validated_plugins(
            self.root, paths.load_config(self.root)["plugins"])
        self.assertEqual(declaration["example"]["path"], wheel)

        (self.root / ".agents-live.toml").write_text(
            '[plugins]\nexample = { path = "../example.whl" }\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "escapes"):
            paths.load_config(self.root)

        (self.root / ".agents-live.toml").write_text(
            '[plugins]\nexample = { path = "Agents/plugins/example.whl", '
            'sha256 = "bad" }\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "64 hexadecimal"):
            paths.load_config(self.root)


class TestRepositoryRegistry(_TempProject):
    def setUp(self) -> None:
        super().setUp()
        self.config_home = self.root / "config-home"
        self._saved_config_home = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.config_home)

    def tearDown(self) -> None:
        if self._saved_config_home is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._saved_config_home
        super().tearDown()

    def test_add_and_default_store_normalized_absolute_path(self) -> None:
        repos._add(str(self.root / "."))
        repos._set_default(str(self.root))
        registry = repos.load()
        self.assertEqual(registry["repos"], {self.root.name: str(self.root)})
        self.assertEqual(registry["default_repo"], self.root.name)
        self.assertEqual(paths.resolve_root(self.root.name), self.root)

    def test_add_registers_under_directory_name(self) -> None:
        repos._add(str(self.root))
        registry = repos.load()
        self.assertEqual(registry["repos"], {self.root.name: str(self.root)})

    def test_registering_returns_the_resolved_root(self) -> None:
        # The caller converges plugins against this path, so it has to be
        # the registered one rather than whatever spelling was typed.
        self.assertEqual(repos._add(str(self.root / ".")), self.root)
        other = self.root / "nested"
        other.mkdir()
        self.assertEqual(repos._set_default(str(other / ".")), other)

    def test_every_registration_path_converges_declared_plugins(self) -> None:
        # init --repo has always registered and converged together; a repo
        # registered through `repos` is no less connected, and a
        # registry-mode repo without its backend reads as fully unowned.
        other = self.root / "nested"
        other.mkdir()
        with mock.patch("agents_live.plugins.converge",
                        return_value=False) as converge:
            repos.main(["add", str(self.root)])
            repos.main(["default", str(other)])
        self.assertEqual(
            [call.args[0] for call in converge.call_args_list],
            [[self.root], [other]])

    def test_registration_survives_a_plugin_that_cannot_be_installed(
            self) -> None:
        # The repo is registered either way; doctor names the plugin
        # problem with its fix, so registration does not unwind.
        with mock.patch("agents_live.plugins.converge",
                        side_effect=OSError("no wheel")):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(repos.main(["add", str(self.root)]), 0)
        self.assertIn("declared plugins could not be installed",
                      err.getvalue())
        self.assertEqual(repos.load()["repos"],
                         {self.root.name: str(self.root)})

    def test_repos_add_is_reachable_from_the_cli(self) -> None:
        # The implementation existed for releases while the command spec
        # omitted it, so the front end rejected the name every user
        # reaches for first.
        spec = next(cmd for cmd in COMMANDS if cmd.name == "repos")
        self.assertIn("add", [child.name for child in spec.subcommands])

    def test_status_says_ownership_is_unavailable_rather_than_blank(
            self) -> None:
        # An unreadable registry and an unowned agent must not render
        # alike: the second reads as a fact about ownership.
        unavailable = status.format_table([{
            "name": "alpha", "type": "cron", "state": "active",
            "runtime": "none", "mode": "plan",
            "schedule": ["0 * * * *"], "ownershipUnavailable": True,
        }])
        self.assertIn("unavailable", unavailable)
        unowned = status.format_table([{
            "name": "alpha", "type": "cron", "state": "active",
            "runtime": "none", "mode": "plan",
            "schedule": ["0 * * * *"], "owner": None, "isOwner": True,
        }])
        self.assertNotIn("unavailable", unowned)

    def test_add_rejects_underivable_directory_name(self) -> None:
        odd = self.root / "-leading-dash"
        odd.mkdir()
        with self.assertRaisesRegex(ValueError, "must start with an alphanumeric"):
            repos._add(str(odd))

    def test_add_rejects_duplicate_path_and_duplicate_name(self) -> None:
        repos._add(str(self.root))
        with self.assertRaisesRegex(ValueError, "already registered as"):
            repos._add(str(self.root / "."))
        clash = self.root / "nested" / self.root.name
        clash.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "already registered"):
            repos._add(str(clash))

    def test_default_and_remove_accept_name_or_path(self) -> None:
        other = self.root / "other-repo"
        other.mkdir()
        repos._add(str(self.root))
        repos._add(str(other))
        repos._set_default(str(other))
        self.assertEqual(repos.load()["default_repo"], "other-repo")
        repos._set_default(self.root.name)
        repos._remove(str(other))
        self.assertNotIn("other-repo", repos.load()["repos"])
        with self.assertRaisesRegex(ValueError, "not a registered repository"):
            repos._remove(str(other))

    def test_the_default_guard_needs_somewhere_else_to_point(self) -> None:
        other = self.root / "other-repo"
        other.mkdir()
        repos._add(str(self.root))
        repos._add(str(other))
        repos._set_default(self.root.name)
        # Two entries: the guard is right, another can inherit the role.
        with self.assertRaisesRegex(ValueError, "is the default"):
            repos._remove(self.root.name)
        repos._remove(str(other))
        # One entry left, and it is the default. Refusing here would be a
        # dead end: `default` has no other candidate to accept, so the
        # registry could never be emptied through the CLI.
        repos._remove(self.root.name)
        registry = repos.load()
        self.assertEqual(registry["repos"], {})
        self.assertIsNone(registry["default_repo"])

    def test_cli_default_registers_unregistered_path(self) -> None:
        self.assertEqual(repos.main(["default", str(self.root)]), 0)
        registry = repos.load()
        self.assertEqual(registry["repos"], {self.root.name: str(self.root)})
        self.assertEqual(registry["default_repo"], self.root.name)

    def test_add_registers_repo_when_declared_wheel_is_missing(self) -> None:
        (self.root / ".agents-live.toml").write_text(
            '[plugins]\nexample-plugin = { path = "dist/missing.whl" }\n',
            encoding="utf-8",
        )
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(repos.main(["add", str(self.root)]), 0)
        self.assertEqual(
            repos.load()["repos"], {self.root.name: str(self.root)})
        self.assertIn(
            "declared plugins could not be installed", stderr.getvalue())

    def test_add_registers_repo_when_declared_wheel_is_invalid(self) -> None:
        wheel = self.root / "dist" / "invalid.whl"
        wheel.parent.mkdir()
        wheel.write_bytes(b"not a wheel")
        (self.root / ".agents-live.toml").write_text(
            '[plugins]\nexample-plugin = { path = "dist/invalid.whl" }\n',
            encoding="utf-8",
        )
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(repos.main(["add", str(self.root)]), 0)
        self.assertEqual(
            repos.load()["repos"], {self.root.name: str(self.root)})
        self.assertIn(
            "declared plugins could not be installed",
            stderr.getvalue())

    def test_help_action_prints_usage(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(repos.main(["help"]), 0)
        self.assertIn("Manage registered repositories", stdout.getvalue())

    def test_local_marker_wins_over_default(self) -> None:
        repos._add(str(self.root))
        repos._set_default(str(self.root))
        os.environ.pop(paths.ENV_VAR, None)
        with tempfile.TemporaryDirectory() as local_tmp:
            local = Path(local_tmp).resolve()
            (local / ".agents-live.toml").write_text("", encoding="utf-8")
            saved = Path.cwd()
            try:
                os.chdir(local)
                paths.clear_cache()
                self.assertEqual(paths.resolve_root(), local)
                self.assertEqual(paths.resolution_source(), "marker")
            finally:
                os.chdir(saved)

    def test_registered_name_wins_over_cwd_directory(self) -> None:
        # A plain --repo name that is registered must mean the registry
        # entry, even when a same-named directory exists under CWD.
        repos._add(str(self.root))
        saved = Path.cwd()
        with tempfile.TemporaryDirectory() as outside:
            decoy = Path(outside) / self.root.name
            decoy.mkdir()
            try:
                os.chdir(outside)
                self.assertEqual(
                    paths.resolve_root(self.root.name), self.root)
            finally:
                os.chdir(saved)

    def test_sole_registered_repo_resolves_without_a_default(self) -> None:
        # `init` always initializes the global workspace, so without this
        # step the one registered project is masked by an empty workspace
        # and read-only views render nothing at all (issue #173).
        repos._add(str(self.root))
        global_root = paths.global_root()
        global_root.mkdir(parents=True)
        (global_root / ".agents-live.toml").write_text("", encoding="utf-8")
        os.environ.pop(paths.ENV_VAR, None)
        with tempfile.TemporaryDirectory() as outside:
            saved = Path.cwd()
            try:
                os.chdir(outside)
                paths.clear_cache()
                self.assertEqual(
                    paths.resolve_root(allow_sole_registered=True), self.root)
                self.assertEqual(paths.resolution_source(), "sole-registered")
            finally:
                os.chdir(saved)

    def _sole_registered_outside_any_project(self, registered: Path) -> None:
        """Register *registered* and initialize the global workspace."""
        repos._add(str(registered))
        global_root = paths.global_root()
        global_root.mkdir(parents=True)
        (global_root / ".agents-live.toml").write_text("", encoding="utf-8")
        os.environ.pop(paths.ENV_VAR, None)

    def test_sole_registered_repo_is_not_offered_to_other_callers(self) -> None:
        # The fallback is a convenience for read-only views. Handing it to
        # every caller would let `start`, `stop`, `delete` and `migrate`
        # write triggers into a project the invocation never named, which
        # is the whole of issue #192.
        self._sole_registered_outside_any_project(self.root)
        with tempfile.TemporaryDirectory() as outside:
            saved = Path.cwd()
            try:
                os.chdir(outside)
                paths.clear_cache()
                self.assertEqual(paths.resolve_root(), paths.global_root())
                self.assertEqual(paths.resolution_source(), "global")
            finally:
                os.chdir(saved)

    def test_a_cached_registry_fallback_is_not_reused_by_other_callers(
            self) -> None:
        # Resolution is cached per process, so a read-only caller running
        # first must not leave the registry answer lying around where a
        # mutating one would pick it up.
        self._sole_registered_outside_any_project(self.root)
        with tempfile.TemporaryDirectory() as outside:
            saved = Path.cwd()
            try:
                os.chdir(outside)
                paths.clear_cache()
                self.assertEqual(
                    paths.resolve_root(allow_sole_registered=True), self.root)
                with self.assertRaisesRegex(
                        ValueError, "no project root found"):
                    paths.resolve_root()
            finally:
                os.chdir(saved)

    def test_a_moved_sole_repo_falls_through_to_the_remaining_resolution(
            self) -> None:
        # The step is a convenience, so an unusable entry must not turn a
        # missing root into a failure about an alias nobody mentioned.
        registered = self.root / "registered-then-moved"
        registered.mkdir()
        self._sole_registered_outside_any_project(registered)
        shutil.rmtree(registered)
        with tempfile.TemporaryDirectory() as outside:
            saved = Path.cwd()
            try:
                os.chdir(outside)
                paths.clear_cache()
                self.assertEqual(
                    paths.resolve_root(allow_sole_registered=True),
                    paths.global_root())
            finally:
                os.chdir(saved)

    def test_several_registered_repos_without_a_default_say_how_to_choose(self) -> None:
        # Two registrations are ambiguous: guessing one would be worse
        # than failing, so the failure has to name the way to select one.
        other = self.root / "other-repo"
        other.mkdir()
        repos._add(str(self.root))
        repos._add(str(other))
        os.environ.pop(paths.ENV_VAR, None)
        with tempfile.TemporaryDirectory() as outside:
            saved = Path.cwd()
            try:
                os.chdir(outside)
                paths.clear_cache()
                with self.assertRaisesRegex(ValueError, "repos default"):
                    paths.resolve_root()
            finally:
                os.chdir(saved)

    def test_configured_default_precedes_global_fallback(self) -> None:
        repos._add(str(self.root))
        repos._set_default(str(self.root))
        global_root = paths.global_root()
        global_root.mkdir(parents=True)
        (global_root / ".agents-live.toml").write_text("", encoding="utf-8")
        os.environ.pop(paths.ENV_VAR, None)
        with tempfile.TemporaryDirectory() as outside:
            saved = Path.cwd()
            try:
                os.chdir(outside)
                paths.clear_cache()
                self.assertEqual(paths.resolve_root(), self.root)
                self.assertEqual(paths.resolution_source(), "default")
            finally:
                os.chdir(saved)

    def test_unavailable_default_fails_actionably(self) -> None:
        missing = self.root / "gone"
        missing.mkdir()
        repos._add(str(missing))
        repos._set_default("gone")
        missing.rmdir()
        with self.assertRaisesRegex(ValueError, "registered repo 'gone'"):
            repos.default_root()

    def test_status_aggregation_qualifies_names_and_keeps_errors(self) -> None:
        with (
            mock.patch.object(
                repos, "entries",
                return_value=[
                    ("life", "/life", None),
                    ("gone", "/gone", "registered repo 'gone' is unavailable"),
                ],
            ),
            mock.patch.object(
                repos, "_child_json",
                return_value={
                    "name": "life", "path": "/life", "ok": True,
                    "result": {"agents": [{"name": "shared", "state": "inactive"}]},
                },
            ),
        ):
            payload = repos.collect_status()
        self.assertFalse(payload["ok"])
        self.assertEqual(
            payload["repos"][0]["result"]["agents"][0]["name"], "life/shared")
        self.assertIn("error", payload["repos"][1])

    def test_child_launch_failure_becomes_error_row(self) -> None:
        # A child that cannot even spawn is that repo's error row; it
        # must never abort the whole aggregate.
        def fake_child(alias: str, path: str, command: str) -> dict:
            if alias == "boom":
                raise FileNotFoundError("agents-live shim missing")
            return {"name": alias, "path": path, "ok": True,
                    "result": {"agents": []}}

        with (
            mock.patch.object(
                repos, "entries",
                return_value=[("boom", "/boom", None), ("ok", "/ok", None)],
            ),
            mock.patch.object(repos, "_child_json", side_effect=fake_child),
        ):
            payload = repos.collect_status()
        self.assertFalse(payload["ok"])
        by_name = {item["name"]: item for item in payload["repos"]}
        self.assertIn("shim missing", by_name["boom"]["error"])
        self.assertTrue(by_name["ok"]["ok"])

    def test_agent_directories_cannot_escape_repository(self) -> None:
        anchored = ["/tmp/agents", str(Path(tempfile.gettempdir()) / "agents")]
        if sys.platform == "win32":
            # Neither spelling is is_absolute() on Windows - one is
            # rooted but driveless, the other drive-relative - so only
            # an anchor check catches them. On POSIX they are ordinary
            # relative names that stay inside the repository.
            anchored += ["\\tmp\\agents", "C:agents"]
        for spelling in anchored:
            with self.assertRaisesRegex(ValueError, "repo-relative"):
                paths.validated_agent_directories(self.root, [spelling])
        with self.assertRaisesRegex(ValueError, "escapes"):
            paths.validated_agent_directories(self.root, ["../agents"])
        with tempfile.TemporaryDirectory() as outside:
            link = self.root / "linked-agents"
            link_directory(link, Path(outside))
            with self.assertRaisesRegex(ValueError, "escapes"):
                paths.validated_agent_directories(self.root, ["linked-agents"])


class TestOwnershipKernel(_TempProject):
    def test_greenfield_is_local(self) -> None:
        self.assertEqual(ownership.mode(), "local")
        self.assertEqual(ownership.load_owners(rate_limit_secs=10**9), {})

    def test_declared_registry_fails_closed_without_state(self) -> None:
        (self.root / ".agents-live.toml").write_text(
            'ownership = "registry"\n', encoding="utf-8")
        # With no backend installed OR with a backend but no registry
        # document, the outcome is identical: abstention, never local.
        with self.assertRaises(ownership.OwnershipUnavailableError):
            ownership.load_owners(rate_limit_secs=10**9)


class TestRuntimeIdentity(_TempProject):
    """Who this runtime says it is, and which owner values it matches."""

    def _as_runtime(self, host: str, runtime: str):
        """Pretend this runtime is ``host``/``runtime``."""
        return (
            mock.patch.object(ownership, "current_host", return_value=host),
            mock.patch.object(hostruntime, "runtime_name",
                              return_value=runtime),
        )

    def test_an_identity_names_the_host_the_runtime_and_a_uuid(self) -> None:
        named, runtime = self._as_runtime("some-host", "ubuntu")
        with named, runtime:
            owner = ownership.current_owner_id()
        host, _, rest = owner.partition("/")
        runtime_part, _, identity = rest.partition("/")
        self.assertEqual(host, "some-host")
        self.assertEqual(runtime_part, "ubuntu")
        self.assertRegex(identity, r"^[0-9a-f]{32}$")

    def test_the_generated_identity_survives_the_command_that_made_it(self) -> None:
        named, runtime = self._as_runtime("some-host", "ubuntu")
        with named, runtime:
            first = ownership.current_owner_id()
            second = ownership.current_owner_id()
        self.assertEqual(first, second)
        stored = (paths.state_home() / ownership.RUNTIME_ID_FILE).read_text(
            encoding="utf-8")
        self.assertEqual(first, f"some-host/ubuntu/{stored}")

    def test_two_distros_on_one_machine_are_distinct_owners(self) -> None:
        # The case a bare hostname could not express: a WSL distro's
        # hostname defaults to the Windows computer name, so the distro
        # name is what separates the rows and the uuid is what separates
        # the claims.
        named, runtime = self._as_runtime("shared-name", "ubuntu")
        with named, runtime:
            first = ownership.current_owner_id()
        (paths.state_home() / ownership.RUNTIME_ID_FILE).unlink()
        named, runtime = self._as_runtime("shared-name", "debian")
        with named, runtime:
            second = ownership.current_owner_id()
            self.assertTrue(ownership.owns(second))
            self.assertFalse(ownership.owns(first))
        self.assertNotEqual(first, second)

    def test_an_unreadable_identity_abstains_rather_than_guesses(self) -> None:
        state = paths.state_home()
        state.mkdir(parents=True, exist_ok=True)
        (state / ownership.RUNTIME_ID_FILE).write_text(
            "not-a-uuid", encoding="utf-8")
        with self.assertRaises(ownership.OwnershipUnavailableError):
            ownership.current_owner_id()

    def test_state_lands_where_the_host_keeps_per_user_state(self) -> None:
        base = hostruntime.user_state_base()
        self.assertTrue(base.is_absolute())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XDG_STATE_HOME", None)
            self.assertEqual(paths.state_home(), base / "agents-live")

    def test_display_keeps_the_readable_half_and_drops_the_uuid(self) -> None:
        self.assertEqual(
            ownership.display_owner("some-host/ubuntu/" + "ab" * 16),
            "some-host/ubuntu")
        self.assertEqual(ownership.display_owner("*"), "*")

    def test_display_shows_an_incomplete_identity_as_it_is(self) -> None:
        # A value that predates the triple, or one a hand-edit truncated,
        # still names a host. Showing "some-host/" rather than inventing a
        # runtime is what tells the reader the row is not matchable.
        self.assertEqual(ownership.display_owner("some-host"), "some-host/")

    def test_only_an_exact_uuid_or_the_wildcard_owns(self) -> None:
        owner = ownership.current_owner_id()
        self.assertTrue(ownership.owns(owner))
        self.assertTrue(ownership.owns(ownership.WILDCARD))
        for stranger in (
            "some-host",                          # no runtime, no uuid
            "some-host/ubuntu",                   # no uuid
            "some-host/ubuntu/" + "ab" * 16,      # someone else's uuid
            "some-host/ubuntu/not-a-uuid",        # unreadable uuid
            "some-host/ubuntu/extra/" + "ab" * 16,  # more parts than parsed
            "",
        ):
            self.assertFalse(ownership.owns(stranger), stranger)

    def test_a_value_that_cannot_be_matched_is_not_ours(self) -> None:
        # The durability rule, stated as one assertion: anything the
        # matcher cannot reduce to a uuid belongs to someone else, so it
        # neither runs here nor gets cleaned up here.
        self.assertEqual(ownership.owner_uuid("some-host"), "")
        self.assertEqual(ownership.owner_uuid("some-host/ubuntu"), "")
        self.assertEqual(
            ownership.owner_uuid("some-host/ubuntu/" + "AB" * 16),
            "ab" * 16)


class TestStartOwnership(_TempProject):
    def setUp(self) -> None:
        super().setUp()
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        self.config = headless.load_agent_config("smoke-fixture")

    def _ownership_context(self):
        return (
            mock.patch.object(ownership, "local_only", return_value=False),
            mock.patch.object(ownership, "current_owner_id",
                              return_value="current-host"),
            mock.patch.object(ownership, "load_owners",
                              return_value={"smoke-fixture": "owning-host"}),
            mock.patch.object(ownership, "set_owner"),
            mock.patch.object(activate, "log_event"),
        )

    def test_interactive_start_prompts_before_takeover(self) -> None:
        local, host, load, set_owner, log = self._ownership_context()
        with (
            local, host, load, set_owner as set_owner_mock, log,
            mock.patch.object(activate.sys, "stdin",
                              mock.Mock(isatty=mock.Mock(return_value=True))),
            mock.patch.object(activate.sys, "stdout",
                              mock.Mock(isatty=mock.Mock(return_value=True))),
            mock.patch("builtins.input", return_value="y") as prompt,
        ):
            self.assertTrue(activate._resolve_activation_ownership(
                self.config, batch_mode=False, transfer_to=None))
        prompt.assert_called_once_with(
            "smoke-fixture is owned by owning-host/; "
            "take ownership and activate here? [y/N] ")
        set_owner_mock.assert_called_once_with("smoke-fixture", "current-host")

    def test_yes_bypasses_takeover_prompt(self) -> None:
        local, host, load, set_owner, log = self._ownership_context()
        with (
            local, host, load, set_owner as set_owner_mock, log,
            mock.patch("builtins.input") as prompt,
        ):
            self.assertTrue(activate._resolve_activation_ownership(
                self.config, batch_mode=False, transfer_to=None,
                assume_yes=True))
        prompt.assert_not_called()
        set_owner_mock.assert_called_once_with("smoke-fixture", "current-host")

    def test_non_tty_start_refuses_takeover(self) -> None:
        local, host, load, set_owner, log = self._ownership_context()
        with (
            local, host, load, set_owner as set_owner_mock, log,
            mock.patch.object(activate.sys, "stdin",
                              mock.Mock(isatty=mock.Mock(return_value=False))),
            mock.patch("builtins.input") as prompt,
        ):
            self.assertFalse(activate._resolve_activation_ownership(
                self.config, batch_mode=False, transfer_to=None))
        prompt.assert_not_called()
        set_owner_mock.assert_not_called()

    def test_yes_does_not_mask_unavailable_registry(self) -> None:
        with (
            mock.patch.object(ownership, "local_only", return_value=False),
            mock.patch.object(
                ownership, "load_owners",
                side_effect=ownership.OwnershipUnavailableError("unavailable")),
        ):
            with self.assertRaises(ownership.OwnershipUnavailableError):
                activate._resolve_activation_ownership(
                    self.config, batch_mode=False, transfer_to=None,
                    assume_yes=True)

    def test_all_rejects_yes(self) -> None:
        with (
            mock.patch("sys.argv", ["agents-live start", "--all", "--yes"]),
            self.assertRaises(SystemExit) as raised,
        ):
            activate.main()
        self.assertEqual(raised.exception.code, 2)

    def test_transfer_here_claims_without_spelling_the_identity(self) -> None:
        # --transfer-to needs a full hostname/runtime/uuid triple, which
        # is only obtainable by copying it out of agent-owners.json.
        # --transfer-here is the same operation for the one identity a
        # runtime can always name: its own.
        with (
            mock.patch("sys.argv",
                       ["agents-live start", "--name", "smoke-fixture",
                        "--transfer-here"]),
            mock.patch.object(ownership, "local_only", return_value=False),
            mock.patch.object(ownership, "load_owners",
                              return_value={"smoke-fixture": "elsewhere"}),
            mock.patch.object(ownership, "set_owner") as set_owner,
            mock.patch.object(activate, "log_event"),
        ):
            activate.main()
        set_owner.assert_called_once_with(
            "smoke-fixture", ownership.current_owner_id())

    def test_transfer_here_and_transfer_to_are_alternatives(self) -> None:
        with (
            mock.patch("sys.argv",
                       ["agents-live start", "--name", "smoke-fixture",
                        "--transfer-here", "--transfer-to", "a/b/c"]),
            self.assertRaises(SystemExit) as raised,
        ):
            activate.main()
        self.assertEqual(raised.exception.code, 2)


class TestOwnershipEnforcement(_TempProject):
    """What the health sweep does with each kind of owner value."""

    def _sweep(self, owner: str | None) -> tuple[list[str], bool]:
        owners = {} if owner is None else {"smoke-fixture": owner}
        states = {"smoke-fixture": {"state": "active"}}
        with (
            mock.patch.object(ownership, "load_owners", return_value=owners),
            mock.patch.object(health_check, "_lifecycle", return_value=True),
            mock.patch.object(health_check, "_err"),
        ):
            return health_check._enforce_ownership(states, [])

    def test_an_unclaimed_agent_keeps_running_here(self) -> None:
        # Absent is not "someone else's" - it is the state every agent is
        # in before the first claim, and the state all of them are in when
        # the repository runs without a registry at all.
        self.assertEqual(self._sweep(None), ([], False))

    def test_this_runtime_keeps_the_agents_it_owns(self) -> None:
        self.assertEqual(
            self._sweep(ownership.current_owner_id()), ([], False))
        self.assertEqual(self._sweep(ownership.WILDCARD), ([], False))

    def test_another_runtimes_agent_is_deactivated_here(self) -> None:
        self.assertEqual(
            self._sweep("some-host/ubuntu/" + "ab" * 16),
            (["smoke-fixture"], False))

    def test_an_unmatchable_value_is_treated_as_someone_elses(self) -> None:
        # A value the matcher cannot reduce to a uuid - truncated, hand
        # edited, restored from a stale backup - stops the agent here
        # rather than letting an unverifiable claim run. Recovering is a
        # deliberate `start --name <agent> --transfer-here`.
        self.assertEqual(self._sweep("some-host"),
                         (["smoke-fixture"], False))

    def test_unreadable_ownership_abstains_and_flags_degraded(self) -> None:
        with (
            mock.patch.object(
                ownership, "load_owners",
                side_effect=ownership.OwnershipUnavailableError("down")),
            mock.patch.object(health_check, "_err"),
        ):
            self.assertEqual(
                health_check._enforce_ownership(
                    {"smoke-fixture": {"state": "active"}}, []),
                ([], True))

    def test_doctor_scopes_cli_warnings_by_the_same_matcher(self) -> None:
        # doctor decides which agent CLIs this host actually needs by
        # reading owner values. It used to compare them to a hostname it
        # spelled itself, which the triple would have made false for every
        # claimed agent - silently dropping the CLI checks that matter.
        self.write_agent(
            "claude-fixture",
            AGENT_DEFINITION.replace("runtime: none", "runtime: claude"))
        owners = self.root / "Agents" / "data" / "agent-owners.json"
        owners.parent.mkdir(parents=True, exist_ok=True)

        def needed_when(owner: str) -> list[str]:
            owners.write_text(
                json.dumps({"owners": {"claude-fixture": owner}}),
                encoding="utf-8")
            with mock.patch.object(doctor, "REPO", self.root):
                buckets = doctor._agent_cli_needed_by_host()
            return buckets["claude"]["owned"]

        self.assertEqual(needed_when(ownership.current_owner_id()),
                         ["claude-fixture"])
        self.assertEqual(needed_when(ownership.WILDCARD), ["claude-fixture"])
        self.assertEqual(needed_when("some-host/ubuntu/" + "ab" * 16), [])
        self.assertEqual(needed_when("some-host"), [])



class TestProjectPlugins(_TempProject):
    def _wheel(self, name: str = "example-plugin", version: str = "1.2.3") -> Path:
        wheel = (
            self.root / "Agents" / "plugins"
            / f"{name}-{version}-py3-none-any.whl")
        wheel.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                f"{name.replace('-', '_')}-{version}.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            )
        return wheel

    def test_declared_plugin_uses_wheel_metadata_identity(self) -> None:
        version = "1.2.3"
        wheel = self._wheel(version=version)
        (self.root / ".agents-live.toml").write_text(
            f'[plugins]\nexample-plugin = '
            f'{{ path = "{wheel.relative_to(self.root).as_posix()}" }}\n',
            encoding="utf-8",
        )
        plugin = plugins.declared(self.root)["example-plugin"]
        self.assertEqual(plugin.name, "example-plugin")
        self.assertEqual(plugin.version, version)

    def test_doctor_fails_for_missing_plugin_and_registry_backend(self) -> None:
        wheel = self._wheel()
        (self.root / ".agents-live.toml").write_text(
            'ownership = "registry"\n'
            f'[plugins]\nexample-plugin = '
            f'{{ path = "{wheel.relative_to(self.root).as_posix()}" }}\n',
            encoding="utf-8",
        )
        no_crontab = subprocess.CompletedProcess(
            ["crontab", "-l"], 1, stdout="", stderr="no crontab for test")
        with (
            mock.patch.object(doctor, "REPO", self.root),
            mock.patch.object(doctor, "_project_checks_enabled", return_value=True),
            mock.patch.object(doctor, "_has", return_value=True),
            mock.patch.object(doctor, "_python_312_resolvable", return_value=True),
            mock.patch.object(hostruntime, "id",
                              return_value=hostruntime.LINUX),
            mock.patch.object(doctor, "_hostname", return_value="test-host"),
            mock.patch.object(doctor, "_package_checks", return_value=[]),
            mock.patch.object(doctor, "_native_agents", return_value=None),
            mock.patch.object(doctor.subprocess, "run", return_value=no_crontab),
            mock.patch.object(
                plugins, "checks",
                return_value=[(
                    "example-plugin", False,
                    "distribution example-plugin is not installed")]),
            mock.patch.object(ownership, "registry_available", return_value=False),
        ):
            checks = {check["name"]: check for check in doctor.collect()}
        plugin_check = checks[
            "plugin example-plugin installed and entry points resolve"]
        self.assertFalse(plugin_check["ok"])
        self.assertTrue(plugin_check["required"])
        self.assertEqual(
            plugin_check["fix"],
            "run `agents-live upgrade` to converge declared plugins",
        )
        self.assertFalse(checks["registry ownership backend resolves"]["ok"])

    def test_init_and_start_converge_but_start_dry_run_does_not(self) -> None:
        os.environ[cli.INIT_REPO_ENV_VAR] = str(self.root)
        with (
            mock.patch.object(plugins, "converge", return_value=False) as converge,
            mock.patch.object(init, "install_skill", return_value=None),
            mock.patch.object(
                completions, "update_best_effort", return_value=True) as update,
            mock.patch.object(
                health_check, "ensure_health_cron_lines", return_value=True),
            mock.patch.object(
                heartbeat, "install_best_effort", return_value=True) as beat,
            mock.patch("importlib.reload", return_value=mock.Mock(
                main=mock.Mock(return_value=0))),
            mock.patch("sys.argv", ["agents-live init"]),
            mock.patch("sys.stdout", new_callable=io.StringIO) as init_stdout,
        ):
            self.assertEqual(init.main(), 0)
        converge.assert_called_once_with(
            [paths.global_root(), self.root], trigger="init")
        update.assert_called_once_with("init")
        beat.assert_called_once_with("init")
        self.assertEqual(repos.default_root(), self.root)
        self.assertIn(
            "into .claude/agents/<agent-name>.md", init_stdout.getvalue())
        self.assertNotIn(
            "into Agents/<agent-name>.md", init_stdout.getvalue())

        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        with (
            mock.patch.object(plugins, "converge", return_value=False) as converge,
            mock.patch.object(activate, "activate_one", return_value=["cron"]),
            mock.patch("sys.argv", ["agents-live start", "--name", "smoke-fixture"]),
        ):
            self.assertEqual(activate.main(), 0)
        converge.assert_called_once_with([self.root], trigger="activate")

        with (
            mock.patch.object(plugins, "converge") as converge,
            mock.patch.object(activate, "activate_one", return_value=["cron"]),
            mock.patch(
                "sys.argv",
                ["agents-live start", "--name", "smoke-fixture", "--dry-run"]),
        ):
            self.assertEqual(activate.main(), 0)
        converge.assert_not_called()

    def test_init_reports_inaccessible_crontab_without_traceback(self) -> None:
        os.environ[cli.INIT_REPO_ENV_VAR] = str(self.root)
        with (
            mock.patch.object(plugins, "converge", return_value=False),
            mock.patch.object(init, "install_skill", return_value=None),
            mock.patch.object(
                health_check, "ensure_health_cron_lines",
                side_effect=health_check.AgentsLiveError(
                    "crontab is not accessible")),
            mock.patch("sys.argv", ["agents-live init"]),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(init.main(), 1)
        self.assertIn(
            "automatic maintenance setup failed: crontab is not accessible",
            stderr.getvalue(),
        )

    def test_converge_skips_missing_wheel_for_installed_plugin(self) -> None:
        (self.root / ".agents-live.toml").write_text(
            '[plugins]\nexample-plugin = { path = "dist/missing.whl", '
            f'sha256 = "{"a" * 64}" }}\n',
            encoding="utf-8",
        )
        entry_point = mock.Mock(
            group="agents_live.agents", name="example",
            load=mock.Mock(return_value=object()),
        )
        distribution = mock.Mock(version="1.2.3", entry_points=[entry_point])
        with mock.patch.object(
                plugins.importlib.metadata, "distribution",
                return_value=distribution):
            self.assertFalse(plugins.converge([self.root]))

    def test_converge_requires_missing_wheel_for_pending_plugin(self) -> None:
        (self.root / ".agents-live.toml").write_text(
            '[plugins]\nexample-plugin = { path = "dist/missing.whl" }\n',
            encoding="utf-8",
        )
        with (
            mock.patch.object(
                plugins.importlib.metadata, "distribution",
                side_effect=plugins.importlib.metadata.PackageNotFoundError),
            self.assertRaisesRegex(plugins.PluginError, "wheel does not exist"),
        ):
            plugins.converge([self.root])

    def test_converge_skips_invalid_wheel_for_installed_plugin(self) -> None:
        wheel = self.root / "dist" / "invalid.whl"
        wheel.parent.mkdir()
        wheel.write_bytes(b"not a wheel")
        (self.root / ".agents-live.toml").write_text(
            '[plugins]\nexample-plugin = { path = "dist/invalid.whl" }\n',
            encoding="utf-8",
        )
        entry_point = mock.Mock(
            group="agents_live.agents", name="example",
            load=mock.Mock(return_value=object()),
        )
        distribution = mock.Mock(version="1.2.3", entry_points=[entry_point])
        with mock.patch.object(
                plugins.importlib.metadata, "distribution",
                return_value=distribution):
            self.assertFalse(plugins.converge([self.root]))
        with self.assertRaisesRegex(plugins.PluginError, "wheel is unreadable"):
            plugins.checks(self.root)

    def test_converge_skips_name_mismatch_for_installed_plugin(self) -> None:
        wheel = self._wheel(name="other-plugin")
        (self.root / ".agents-live.toml").write_text(
            f'[plugins]\nexample-plugin = '
            f'{{ path = "{wheel.relative_to(self.root).as_posix()}" }}\n',
            encoding="utf-8",
        )
        entry_point = mock.Mock(
            group="agents_live.agents", name="example",
            load=mock.Mock(return_value=object()),
        )
        distribution = mock.Mock(version="1.2.3", entry_points=[entry_point])
        with mock.patch.object(
                plugins.importlib.metadata, "distribution",
                return_value=distribution):
            self.assertFalse(plugins.converge([self.root]))

    def test_union_prefers_available_wheel_metadata(self) -> None:
        (self.root / ".agents-live.toml").write_text(
            '[plugins]\nexample-plugin = { path = "dist/missing.whl" }\n',
            encoding="utf-8",
        )
        with tempfile.TemporaryDirectory() as other_tmp:
            other = Path(other_tmp).resolve()
            wheel = (
                other / "dist" / "example_plugin-3.2.1-py3-none-any.whl")
            wheel.parent.mkdir()
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "example_plugin-3.2.1.dist-info/METADATA",
                    "Metadata-Version: 2.1\nName: example-plugin\n"
                    "Version: 3.2.1\n",
                )
            (other / ".agents-live.toml").write_text(
                '[plugins]\nexample-plugin = '
                '{ path = "dist/example_plugin-3.2.1-py3-none-any.whl" }\n',
                encoding="utf-8",
            )
            plugin = plugins.union([self.root, other])["example-plugin"]
            self.assertEqual(plugin.version, "3.2.1")
            self.assertEqual(plugin.path, wheel)

    def test_union_rejects_conflicting_checksum_without_wheel(self) -> None:
        (self.root / ".agents-live.toml").write_text(
            '[plugins]\nexample-plugin = { path = "dist/missing.whl", '
            f'sha256 = "{"a" * 64}" }}\n',
            encoding="utf-8",
        )
        with tempfile.TemporaryDirectory() as other_tmp:
            other = Path(other_tmp).resolve()
            wheel = other / "dist" / "example_plugin-1.0-py3-none-any.whl"
            wheel.parent.mkdir()
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "example_plugin-1.0.dist-info/METADATA",
                    "Metadata-Version: 2.1\nName: example-plugin\n"
                    "Version: 1.0\n",
                )
            (other / ".agents-live.toml").write_text(
                '[plugins]\nexample-plugin = '
                '{ path = "dist/example_plugin-1.0-py3-none-any.whl", '
                f'sha256 = "{"b" * 64}" }}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                    plugins.PluginError, "conflicting sha256"):
                plugins.union([self.root, other])

    def test_union_preserves_checksum_from_identical_declaration(self) -> None:
        first = self._wheel()
        digest = hashlib.sha256(first.read_bytes()).hexdigest()
        (self.root / ".agents-live.toml").write_text(
            f'[plugins]\nexample-plugin = '
            f'{{ path = "{first.relative_to(self.root).as_posix()}" }}\n',
            encoding="utf-8",
        )
        with tempfile.TemporaryDirectory() as other_tmp:
            other = Path(other_tmp).resolve()
            second = other / "dist" / first.name
            second.parent.mkdir()
            second.write_bytes(first.read_bytes())
            (other / ".agents-live.toml").write_text(
                f'[plugins]\nexample-plugin = '
                f'{{ path = "{second.relative_to(other).as_posix()}", '
                f'sha256 = "{digest}" }}\n',
                encoding="utf-8",
            )
            plugin = plugins.union([self.root, other])["example-plugin"]
        self.assertEqual(plugin.sha256, digest)

    def test_pending_install_validates_installed_plugin_wheel(self) -> None:
        invalid = plugins.Plugin(
            "installed-plugin", self.root / "missing.whl", None, None)
        pending_wheel = self.root / "pending.whl"
        pending_wheel.write_bytes(b"pending")
        pending = plugins.Plugin(
            "pending-plugin", pending_wheel, None, "1.0")
        with (
            mock.patch.object(
                plugins, "union",
                return_value={"installed-plugin": invalid, "pending-plugin": pending}),
            mock.patch.object(
                plugins, "_installed_state",
                side_effect=[(True, "installed"), (False, "missing")]),
            self.assertRaisesRegex(plugins.PluginError, "wheel does not exist"),
        ):
            plugins.converge([self.root])


class TestRuntimeInstallCommand(unittest.TestCase):
    """What `upgrade` asks uv to install, without asking uv.

    Constructed commands are asserted directly rather than through a
    patched subprocess: a test that patches the call can only confirm
    the caller agrees with the author's belief about it, which is how a
    release gated the wrong project through seventeen passing tests
    (#184).
    """

    def test_no_source_upgrades_the_published_package(self) -> None:
        self.assertEqual(
            upgrade._install_command("uv", None),
            ["uv", "tool", "upgrade", "agents-live"])

    def test_a_source_installs_that_source_over_the_installed_tool(
            self) -> None:
        command = upgrade._install_command("uv", Path("/build/agents-live"))
        # --force is what makes this replace an existing install rather
        # than fail as already-present, and the source has to be the
        # path asked for rather than the package name.
        self.assertIn("--force", command)
        self.assertIn(str(Path("/build/agents-live")), command)
        self.assertNotIn("upgrade", command)

    def test_a_local_source_never_reaches_the_index(self) -> None:
        # The package name may appear as the thing to rebuild, but never
        # as the thing to install: a bare "agents-live" target would
        # silently install the published release while the command
        # reported the local build.
        source = Path("/build/agents-live")
        command = upgrade._install_command("uv", source)
        self.assertEqual(command[-1], str(source))
        named = [i for i, arg in enumerate(command) if arg == "agents-live"]
        for index in named:
            self.assertEqual(command[index - 1], "--reinstall-package")

    def test_a_local_source_is_rebuilt_rather_than_served_from_cache(
            self) -> None:
        # uv will reuse a cached build of the same directory, so --force
        # alone can install the *previous* source and report success.
        # Installing a stale build is the one outcome --from exists to
        # rule out. Scoped to the package so dependencies stay cached.
        command = upgrade._install_command("uv", Path("/build/agents-live"))
        self.assertIn("--reinstall-package", command)
        self.assertNotIn("--reinstall", command)


class TestAdminLog(_TempProject):
    """Host-scoped records of the operations that change this host."""

    def events(self) -> list[dict]:
        path = adminlog.log_path()
        if not path.is_file():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def only(self, operation: str) -> list[dict]:
        return [e for e in self.events() if e.get("operation") == operation]

    def test_record_carries_the_host_scope_and_invoking_command(self) -> None:
        with mock.patch.object(sys, "argv", ["/usr/bin/agents-live", "repos", "add"]):
            adminlog.record("repo-register", repo="alpha")
        (event,) = self.events()
        self.assertEqual(event["scope"], "host")
        self.assertEqual(event["agent_name"], "admin")
        self.assertEqual(event["operation"], "repo-register")
        self.assertEqual(event["command"], "agents-live repos add")
        self.assertEqual(event["repo"], "alpha")
        self.assertEqual(event["log_schema"], 5)
        self.assertIn("interactive", event)
        # `timeline` renders by phase and message: one shared phase keeps
        # administration a single legible track, the operation names the verb.
        self.assertEqual(event["phase"], "admin")
        self.assertEqual(event["message"], "repo-register")
        # Host-scoped, so the readers that union the host log directory
        # pick it up with no reader change.
        self.assertEqual(adminlog.log_path().parent, paths.host_logs_dir())

    def test_record_never_raises_when_the_log_cannot_be_written(self) -> None:
        with mock.patch.object(
                adminlog, "log_path",
                side_effect=OSError("state home is gone")):
            adminlog.record("repo-register", repo="alpha")  # must not raise

    def test_a_credential_on_the_command_line_never_reaches_the_log(self) -> None:
        # `agents-live logs` prints this field back, so a secret passed
        # as an argument would be readable long after the command ran.
        argv = ["/usr/bin/agents-live", "repos", "add", "--token", "s3cr3t",
                "--api-key=hunter2", "--github-token", "ghp_xyz",
                "--name", "alpha"]
        with mock.patch.object(sys, "argv", argv):
            adminlog.record("repo-register", repo="alpha")
        (event,) = self.events()
        self.assertNotIn("s3cr3t", event["command"])
        self.assertNotIn("hunter2", event["command"])
        self.assertNotIn("ghp_xyz", event["command"])
        # The shape of the invocation is the diagnostic and survives.
        self.assertEqual(
            event["command"],
            "agents-live repos add --token *** --api-key=*** "
            "--github-token *** --name alpha")

    def test_operation_pairs_start_and_end_and_records_failure(self) -> None:
        with adminlog.operation("upgrade-runtime", version_before="1.0") as end:
            end["version_after"] = "1.1"
        start, done = self.only("upgrade-runtime")
        self.assertEqual(start["status"], "start")
        self.assertEqual(done["status"], "ok")
        self.assertEqual(done["version_after"], "1.1")
        self.assertIsInstance(done["duration_s"], float)

        with self.assertRaises(ValueError):
            with adminlog.operation("init"):
                raise ValueError("boom")
        failed = self.only("init")[-1]
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["level"], "error")
        self.assertEqual(failed["error_category"], "ValueError")
        self.assertEqual(failed["message"], "boom")

    def test_repository_registration_and_removal_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp).resolve()
            repos.ensure_default(other)
            (registered,) = self.only("repo-register")
            self.assertEqual(registered["repo"], other.name)
            self.assertEqual(registered["root"], str(other))
            (default,) = self.only("repo-default")
            self.assertEqual(default["repo"], other.name)
            repos._remove(other.name)
            (removed,) = self.only("repo-remove")
            self.assertEqual(removed["repo"], other.name)
            self.assertEqual(removed["root"], str(other))

    def test_schedule_install_and_removal_are_recorded(self) -> None:
        spec = triggers.TriggerSpec(
            name="alpha", kind=triggers.SCHEDULE, root=self.root,
            schedules=("0 * * * *",), command=("echo", "alpha"),
            path="/usr/bin")
        with (
            mock.patch.object(headless, "crontab_lock", contextlib.nullcontext),
            mock.patch.object(headless, "current_crontab_lines", return_value=[]),
            mock.patch.object(headless, "install_crontab"),
        ):
            schedules.install(spec)
        (installed,) = self.only("schedule-install")
        self.assertEqual(installed["agent"], "alpha")
        self.assertEqual(installed["scheduler"], hostruntime.CRONTAB)

        with mock.patch.object(
                headless, "remove_cron_entries", return_value=False):
            schedules.remove("alpha")
        self.assertEqual(self.only("schedule-remove"), [])
        with mock.patch.object(
                headless, "remove_cron_entries", return_value=True):
            schedules.remove("alpha")
        (removed,) = self.only("schedule-remove")
        self.assertEqual(removed["agent"], "alpha")

    def test_an_audit_field_never_fails_the_operation_it_records(self) -> None:
        # A crontab removal needs no project root to do its work, so
        # resolving one for the record must not be able to fail it.
        with (
            mock.patch.object(
                schedules, "_root",
                side_effect=ValueError("no project root found")),
            mock.patch.object(
                headless, "remove_cron_entries", return_value=True),
        ):
            self.assertTrue(schedules.remove("alpha"))
        (removed,) = self.only("schedule-remove")
        self.assertEqual(removed["agent"], "alpha")
        self.assertNotIn("root", removed)

        # Same rule for the owner an ownership write is about to replace.
        backend = mock.Mock(
            load_owners=mock.Mock(
                side_effect=ownership.OwnershipUnavailableError("no registry")),
            set_owner=mock.Mock(),
        )
        with (
            mock.patch.object(ownership, "mode", return_value="registry"),
            mock.patch.object(ownership, "_require_backend", return_value=backend),
        ):
            ownership.set_owner("alpha", "new-host/wsl/" + "b" * 32)
        backend.set_owner.assert_called_once()
        (moved,) = self.only("ownership-set")
        self.assertNotIn("owner_from", moved)

    def test_ownership_transfer_records_who_moved_what(self) -> None:
        backend = mock.Mock(
            load_owners=mock.Mock(return_value={"alpha": "old-host/wsl/" + "a" * 32}),
            set_owner=mock.Mock(),
            remove_owner=mock.Mock(return_value=True),
        )
        with (
            mock.patch.object(ownership, "mode", return_value="registry"),
            mock.patch.object(ownership, "_require_backend", return_value=backend),
        ):
            ownership.set_owner("alpha", "new-host/wsl/" + "b" * 32)
            (moved,) = self.only("ownership-set")
            self.assertEqual(moved["agent"], "alpha")
            self.assertEqual(moved["owner_from"], "old-host/wsl/" + "a" * 32)
            self.assertEqual(moved["owner_to"], "new-host/wsl/" + "b" * 32)
            self.assertFalse(moved["claimed"])

            # An unchanged assignment is not a transfer and is not recorded.
            ownership.set_owner("alpha", "old-host/wsl/" + "a" * 32)
            self.assertEqual(len(self.only("ownership-set")), 1)

            ownership.remove_owner("alpha")
            (removed,) = self.only("ownership-remove")
            self.assertEqual(removed["agent"], "alpha")
            self.assertEqual(removed["owner_from"], "old-host/wsl/" + "a" * 32)

    def test_convergence_records_its_trigger_and_versions(self) -> None:
        wheel = self.root / "example.whl"
        wheel.write_bytes(b"example")
        plugin = plugins.Plugin("example-plugin", wheel, None, "1.0")
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            mock.patch.object(
                plugins, "union", return_value={"example-plugin": plugin}),
            mock.patch.object(
                plugins, "_installed_state", return_value=(False, "missing")),
            mock.patch.object(plugins, "_integrity_error", return_value=None),
            mock.patch.object(
                plugins, "_receipt_requirements",
                return_value=(
                    plugins.ReceiptRequirement("agents-live==9.9.9"), {})),
            mock.patch.object(plugins, "find_uv", return_value="/usr/bin/uv"),
            mock.patch.object(plugins, "installed_version", return_value="9.9.9"),
            mock.patch.object(plugins.subprocess, "run", return_value=completed),
        ):
            self.assertTrue(plugins.converge([self.root], trigger="repos-register"))
        start, done = self.only("plugin-converge")
        self.assertEqual(start["trigger"], "repos-register")
        self.assertEqual(start["primary"], "agents-live==9.9.9")
        self.assertEqual(start["plugins"], ["example-plugin==1.0"])
        self.assertEqual(done["status"], "ok")
        self.assertEqual(done["version_after"], "9.9.9")

    def test_convergence_failure_is_recorded_as_an_error(self) -> None:
        wheel = self.root / "example.whl"
        wheel.write_bytes(b"example")
        plugin = plugins.Plugin("example-plugin", wheel, None, "1.0")
        completed = subprocess.CompletedProcess(args=[], returncode=2)
        with (
            mock.patch.object(
                plugins, "union", return_value={"example-plugin": plugin}),
            mock.patch.object(
                plugins, "_installed_state", return_value=(False, "missing")),
            mock.patch.object(plugins, "_integrity_error", return_value=None),
            mock.patch.object(
                plugins, "_receipt_requirements",
                return_value=(
                    plugins.ReceiptRequirement("agents-live==9.9.9"), {})),
            mock.patch.object(plugins, "find_uv", return_value="/usr/bin/uv"),
            mock.patch.object(plugins.subprocess, "run", return_value=completed),
            self.assertRaises(plugins.PluginError),
        ):
            plugins.converge([self.root], trigger="init")
        failed = self.only("plugin-converge")[-1]
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["error_category"], "PluginError")


class TestConvergencePinsTheKernel(_TempProject):
    """Convergence changes plugins; only `upgrade` changes versions."""

    def receipt(self, requirements: list[dict]) -> Path:
        path = self.root / "uv-receipt.toml"
        lines = ["[tool]", "requirements = ["]
        lines.extend(
            "    { " + ", ".join(
                f'{key} = {json.dumps(value)}' for key, value in entry.items()
            ) + " },"
            for entry in requirements)
        lines.append("]")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def resolve(self, requirements: list[dict], **kwargs):
        receipt = self.receipt(requirements)
        with mock.patch.object(plugins, "_receipt_path", return_value=receipt):
            return plugins._receipt_requirements(**kwargs)

    def test_a_bare_primary_is_pinned_to_the_running_version(self) -> None:
        primary, _ = self.resolve([{"name": "agents-live"}])
        self.assertEqual(
            primary.value, f"agents-live=={plugins.__version__}")

    def test_upgrade_opts_out_so_it_can_still_move_the_version(self) -> None:
        primary, _ = self.resolve(
            [{"name": "agents-live"}], pin_primary=False)
        self.assertEqual(primary.value, "agents-live")

    def test_an_explicit_source_or_specifier_is_left_alone(self) -> None:
        primary, _ = self.resolve(
            [{"name": "agents-live", "specifier": "==4.0.0"}])
        self.assertEqual(primary.value, "agents-live==4.0.0")
        primary, _ = self.resolve(
            [{"name": "agents-live", "path": "/checkout", "editable": True}])
        self.assertEqual(primary.value, "/checkout")
        self.assertTrue(primary.editable)

    def test_declared_plugins_are_never_pinned_by_this_rule(self) -> None:
        _, extras = self.resolve([
            {"name": "agents-live"},
            {"name": "example-plugin", "path": "/wheels/example.whl"},
        ])
        self.assertEqual(extras["example-plugin"].value, "/wheels/example.whl")


class TestAgentParsing(_TempProject):
    def test_native_agent_parses(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        config = headless.load_agent_config("smoke-fixture")
        self.assertEqual(config.name, "smoke-fixture")
        self.assertEqual(config.schedule, [TEST_CRON_SCHEDULE])

    def test_json_extraction_prefers_final_valid_fence(self) -> None:
        output = "\n".join([
            '```json\n{"status":"fail","detail":"provisional"}\n```',
            '```json\n{"status":"pass"}\n```',
        ])
        record = headless._extract_json_value(output)
        self.assertEqual(json.loads(record.text), {"status": "pass"})
        self.assertEqual(record.candidate_count, 2)

    def test_watcher_ignores_generated_index_files(self) -> None:
        self.assertTrue(watchpolicy.should_ignore(
            self.root / "Agents" / "notes" / "_index_.md", root=self.root))
        self.assertFalse(watchpolicy.should_ignore(
            self.root / "Agents" / "notes" / "trigger.txt", root=self.root))

    def test_unknown_runtime_fails_closed(self) -> None:
        self.write_agent("bad-runtime", AGENT_DEFINITION.replace("runtime: none",
                                 "runtime: nonsense"))
        with self.assertRaises(headless.AgentsLiveError):
            headless.load_agent_config("bad-runtime")

    def test_schedule_injection_fails_closed(self) -> None:
        # PKG-002: the schedule is embedded verbatim at the head of a
        # crontab line, so anything beyond cron fields is command
        # injection, not configuration.
        for hostile in (
            "* * * * * touch /tmp/pwned; #",
            "@reboot; touch /tmp/pwned",
            "0 6 * * *\n* * * * * touch /tmp/pwned",
            "@daily @daily",
        ):
            self.write_agent("sched", AGENT_DEFINITION.replace(
                f'schedule: "{TEST_CRON_SCHEDULE}"',
                f'schedule: "{hostile.replace(chr(10), chr(92) + "n")}"'))
            with self.assertRaisesRegex(headless.AgentsLiveError,
                                        "invalid schedule"):
                headless.load_agent_config("sched")
        for benign in ("@reboot", "*/5 8-18 * * 1-5", TEST_CRON_SCHEDULE,
                       # Vixie cron name vocabulary is legal and carries
                       # no injection risk (letters only).
                       "0 9 * * MON-FRI", "30 6 * JAN-DEC SUN"):
            self.write_agent("sched", AGENT_DEFINITION.replace(
                TEST_CRON_SCHEDULE, benign))
            config = headless.load_agent_config("sched")
            self.assertEqual(config.schedule, [benign])

    def test_watch_and_processor_paths_stay_inside_repo(self) -> None:
        # PKG-003: watchPath and processors are documented repo-relative.
        self.write_agent("esc", AGENT_DEFINITION + "")
        config = headless.load_agent_config("esc")
        with self.assertRaisesRegex(headless.AgentsLiveError, "outside"):
            config.watch_path_absolute_for("../outside")
        with self.assertRaisesRegex(headless.AgentsLiveError, "outside"):
            config.watch_path_absolute_for("/etc")
        inside = config.watch_path_absolute_for("Agents/data")
        self.assertEqual(inside, self.root / "Agents" / "data")
        escaping = headless.replace(config, pre_processor="../evil.py")
        with self.assertRaisesRegex(headless.AgentsLiveError, "outside"):
            _ = escaping.pre_processor_path


class TestInvocationForms(_TempProject):
    def test_explicit_agent_path_loads_without_agent_directory_lookup(self) -> None:
        prompt = self.root / "my-agent.md"
        prompt.write_text(AGENT_DEFINITION, encoding="utf-8")
        config = headless.load_agent_config(str(prompt))
        self.assertEqual(config.name, "my-agent")
        self.assertEqual(config.prompt_path, prompt)

    def test_cli_canonicalizes_explicit_agent_path_and_pins_cwd(self) -> None:
        prompt = self.root / "my-agent.md"
        prompt.write_text(AGENT_DEFINITION, encoding="utf-8")
        os.environ.pop(paths.ENV_VAR, None)
        dispatched: list[str] = []

        def capture_argv() -> int:
            dispatched.extend(sys.argv)
            return 0

        saved_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with (
                mock.patch.object(preflight, "check", return_value=None),
                mock.patch.object(
                    run, "main", side_effect=capture_argv) as run_main,
            ):
                self.assertEqual(cli.main(["run", "./my-agent.md"]), 0)
        finally:
            os.chdir(saved_cwd)
        run_main.assert_called_once_with()
        self.assertEqual(dispatched[0], "agents-live run")
        self.assertEqual(dispatched[1:], ["--name", str(prompt)])
        self.assertEqual(os.environ[paths.ENV_VAR], str(self.root))

    def test_path_backed_cron_preserves_canonical_agent_file(self) -> None:
        prompt = self.root / "my-agent.md"
        prompt.write_text(AGENT_DEFINITION, encoding="utf-8")
        lines = schedule_lines(str(prompt))
        self.assertIn(str(prompt), lines[0])
        self.assertTrue(headless.cron_line_matches(lines[0], str(prompt)))

    def test_run_invocation_carries_name_token(self) -> None:
        line = f"{TEST_CRON_SCHEDULE} cd {cron_root(self.root)} && " + (
            shlex.join(headless.run_invocation("t")))
        self.assertTrue(headless.cron_line_matches(line, "t"))

    def test_trigger_matching_is_scoped_to_current_repo(self) -> None:
        cron = (f"{TEST_CRON_SCHEDULE} cd {cron_root(self.root)} && "
                "agents-live run --name shared --quiet")
        watcher = watcher_reboot_line("shared")
        self.assertTrue(headless.cron_line_matches(cron, "shared"))
        self.assertFalse(headless.cron_line_matches(
            cron.replace(str(self.root), FOREIGN_REPO), "shared"))
        with mock.patch.object(
                headless, "current_crontab_lines",
                return_value=[watcher.replace(str(self.root), FOREIGN_REPO)]):
            self.assertEqual(headless.list_reboot_watcher_agent_names(), [])

    def test_crontab_lock_fails_fast_when_busy(self) -> None:
        with mock.patch.dict(
                os.environ, {"XDG_STATE_HOME": str(self.root / "state")}):
            with headless.crontab_lock():
                with self.assertRaisesRegex(
                        headless.AgentsLiveError, "crontab is busy"):
                    with headless.crontab_lock():
                        self.fail("contended lock was acquired")

    def test_removal_preserves_foreign_same_named_entries(self) -> None:
        cron = (f"{TEST_CRON_SCHEDULE} cd {cron_root(self.root)} && "
                "agents-live run --name shared --quiet")
        watcher = watcher_reboot_line("shared")
        foreign_cron = cron.replace(str(self.root), FOREIGN_REPO)
        foreign_watcher = watcher.replace(str(self.root), FOREIGN_REPO)
        with (
            mock.patch.dict(
                os.environ, {"XDG_STATE_HOME": str(self.root / "state")}),
            mock.patch.object(
                headless, "current_crontab_lines",
                side_effect=[[foreign_cron, cron], [foreign_watcher, watcher]]),
            mock.patch.object(headless, "install_crontab") as install,
        ):
            self.assertTrue(headless.remove_cron_entries("shared"))
            self.assertTrue(headless.remove_watcher_reboot_line("shared"))
        self.assertEqual(
            install.call_args_list,
            [mock.call([foreign_cron]), mock.call([foreign_watcher])])

    def test_reboot_line_round_trips_agent_name(self) -> None:
        line = watcher_reboot_line("t")
        self.assertIn("internal ensure-watcher t", line)
        self.assertNotIn("start --ensure-watcher", line)

    def test_persisted_lines_carry_inline_path(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        cron_lines = schedule_lines("smoke-fixture")
        watcher_line = watcher_reboot_line("smoke-fixture")
        for line in [*cron_lines, watcher_line]:
            self.assertIn("PATH=", line)
        self.assertTrue(
            headless.cron_line_matches(cron_lines[0], "smoke-fixture"))
        self.assertTrue(headless._watcher_reboot_line_matches(
            watcher_line, "smoke-fixture"))

    def test_install_refuses_unreadable_crontab(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        with (
            mock.patch.dict(
                os.environ, {"XDG_STATE_HOME": str(self.root / "state")}),
            mock.patch.object(headless, "current_crontab_lines",
                              return_value=None),
            mock.patch.object(headless, "install_crontab") as h_install,
            mock.patch.object(activate, "_validate_handler_paths"),
        ):
            with self.assertRaisesRegex(
                    headless.AgentsLiveError, "not accessible"):
                headless.install_watcher_reboot_line("smoke-fixture")
            with self.assertRaisesRegex(
                    headless.AgentsLiveError, "not accessible"):
                activate.install_cron_agent("smoke-fixture")
        h_install.assert_not_called()

    def test_watcher_matching_is_scoped_to_current_repo(self) -> None:
        packaged = ["/home/u/.local/bin/agents-live", "--repo", str(self.root),
                    "internal", "watch-loop", "shared"]
        foreign = ["/home/u/.local/bin/agents-live", "--repo", FOREIGN_REPO,
                   "internal", "watch-loop", "shared"]
        flat = ["uv", "run", "--script",
                f"{self.root}/scripts/activate.py", "watch-loop", "shared"]
        # The forward slash is deliberate: Windows accepts it, and the
        # repo-containment check must not care which separator it sees.
        self.assertTrue(headless._is_watcher_cmdline(packaged, "shared"))
        self.assertTrue(headless._is_watcher_cmdline(flat, "shared"))
        self.assertFalse(headless._is_watcher_cmdline(foreign, "shared"))
        self.assertEqual(
            headless._watcher_cmdline_agent_name(packaged), "shared")
        self.assertIsNone(headless._watcher_cmdline_agent_name(foreign))

    def test_packaged_cron_lines_are_enumerable(self) -> None:
        root = cron_root(self.root)
        packaged = (f"{TEST_CRON_SCHEDULE} cd {root} && "
                    f"/home/u/.local/bin/agents-live --repo {root} "
                    "run --name foo --quiet 2>&1")
        flat = (f"{TEST_CRON_SCHEDULE} cd {root} && uv run --script "
                f"{cron_root(self.root / 'scripts' / 'run.py')} "
                "--name bar --quiet 2>&1")
        unrelated = f"{TEST_CRON_SCHEDULE} cd {root} && /usr/bin/backup"
        self.assertEqual(headless._cron_line_agent_name(packaged), "foo")
        self.assertEqual(headless._cron_line_agent_name(flat), "bar")
        self.assertIsNone(headless._cron_line_agent_name(unrelated))

    def test_interrupted_payload_refresh_is_recoverable(self) -> None:
        source = self.root / "payload-src"
        (source / "docs").mkdir(parents=True)
        (source / "SKILL.md").write_text("new skill\n", encoding="utf-8")
        (source / "VERSION").write_text("2.0.0\n", encoding="utf-8")
        (source / "docs" / "a.md").write_text("new docs\n", encoding="utf-8")
        dest = self.root / ".claude" / "skills" / "agents-live"
        (dest / "docs").mkdir(parents=True)
        (dest / "SKILL.md").write_text("old skill\n", encoding="utf-8")
        (dest / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        (dest / "user-note.md").write_text("mine\n", encoding="utf-8")

        # A copy that dies mid-staging must leave the old payload intact.
        with (
            mock.patch.object(init, "_skill_source", return_value=source),
            mock.patch.object(init, "_copy_payload",
                              side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            init.install_skill(self.root)
        self.assertEqual((dest / "VERSION").read_text(encoding="utf-8"),
                         "1.0.0\n")
        self.assertEqual((dest / "SKILL.md").read_text(encoding="utf-8"),
                         "old skill\n")

        # The real refresh completes, preserves user files, and a
        # rerun reports current.
        with mock.patch.object(init, "_skill_source", return_value=source):
            self.assertEqual(init.install_skill(self.root), "refreshed")
            self.assertIsNone(init.install_skill(self.root))
        self.assertEqual((dest / "VERSION").read_text(encoding="utf-8"),
                         "2.0.0\n")
        self.assertEqual((dest / "user-note.md").read_text(encoding="utf-8"),
                         "mine\n")
        self.assertEqual(
            [p.name for p in dest.parent.iterdir()], ["agents-live"])

    def test_doctor_flags_lines_from_missing_project_roots(self) -> None:
        gone = f"{self.root}-deleted"
        crontab = "\n".join([
            f"{TEST_CRON_SCHEDULE} cd {cron_root(gone)} && "
            f"agents-live --repo {cron_root(gone)} "
            "run --name lost --quiet 2>&1",
            f"{TEST_CRON_SCHEDULE} cd {FOREIGN_REPO} && /usr/bin/backup",
        ])
        completed = subprocess.CompletedProcess(
            ["crontab", "-l"], 0, stdout=crontab, stderr="")
        with (
            mock.patch.object(doctor, "REPO", self.root),
            mock.patch.object(doctor.subprocess, "run",
                              return_value=completed),
        ):
            orphans, stale = doctor._crontab_inconsistencies()
        self.assertEqual(orphans, [])
        self.assertEqual(stale, [f"{gone} (project root moved or deleted)"])

    def test_jsonc_mcp_config_parses_and_fails_closed(self) -> None:
        # PKG-004: inline comments and trailing commas are valid VS Code
        # JSONC; malformed files must raise, never silently drop servers.
        # Layout-agnostic import: resolve the loader through headless.
        loader = importlib.import_module(
            headless.load_mcp_servers.__module__)
        config_dir = self.root / ".vscode"
        config_dir.mkdir()
        (config_dir / "mcp.json").write_text(
            '{\n'
            '  // full-line comment\n'
            '  "servers": {\n'
            '    "custom": {\n'
            '      "command": "npx", // inline comment\n'
            '      "args": ["-y", "custom-mcp",], /* block */\n'
            '    },\n'
            '  },\n'
            '}\n',
            encoding="utf-8")
        servers = loader.load_mcp_servers(self.root)
        self.assertEqual(servers["custom"]["command"], "npx")
        self.assertEqual(servers["custom"]["args"], ["-y", "custom-mcp"])
        for malformed in (
            "{broken",
            '[{"command": "npx"}]',          # top-level array
            '{"servers": {} /* unterminated',  # unterminated block comment
            '{"servers": ["not", "a", "table"]}',
        ):
            (config_dir / "mcp.json").write_text(malformed, encoding="utf-8")
            with self.assertRaises(loader.McpConfigError):
                loader.load_mcp_servers(self.root)

    def test_status_treats_missing_crontab_as_empty_not_sandbox(self) -> None:
        # PKG-005: a fresh user has no crontab; that is not a sandbox.
        fresh = subprocess.CompletedProcess(
            ["crontab", "-l"], 1, stdout="", stderr="no crontab for user\n")
        sandbox = subprocess.CompletedProcess(
            ["crontab", "-l"], 1, stdout="",
            stderr="crontab: not allowed here\n")
        with mock.patch.object(status.subprocess, "run", return_value=fresh):
            self.assertFalse(status._in_sandbox())
        with mock.patch.object(status.subprocess, "run", return_value=sandbox):
            self.assertTrue(status._in_sandbox())

    def test_doctor_skips_unreadable_crontab(self) -> None:
        completed = subprocess.CompletedProcess(
            ["crontab", "-l"], 1, stdout="",
            stderr="crontab: error: cannot open crontab")
        with mock.patch.object(doctor.subprocess, "run",
                               return_value=completed):
            self.assertIsNone(doctor._crontab_inconsistencies())

    def test_install_preserves_user_path_and_foreign_lines(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        user_path = "PATH=/custom/bin:/usr/bin"
        foreign = (f"{TEST_CRON_SCHEDULE} cd {FOREIGN_REPO} && agents-live "
                   f"--repo {FOREIGN_REPO} run --name other --quiet 2>&1")
        with (
            mock.patch.dict(
                os.environ, {"XDG_STATE_HOME": str(self.root / "state")}),
            mock.patch.object(headless, "current_crontab_lines",
                              return_value=[user_path, foreign]),
            mock.patch.object(headless, "install_crontab") as install,
            mock.patch.object(activate, "_validate_handler_paths"),
        ):
            activate.install_cron_agent("smoke-fixture")
        installed = install.call_args[0][0]
        self.assertIn(user_path, installed)
        self.assertIn(foreign, installed)


class TestMigratePlanning(_TempProject):
    def test_canonical_lines_are_no_op(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        canonical = schedule_lines("smoke-fixture")
        plan = migrate.plan_migration(canonical)
        self.assertEqual(plan["schedule"], {})
        self.assertEqual(plan["missing"], [])

    def test_stale_line_planned_for_rewrite(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        stale = (f"{TEST_CRON_SCHEDULE} cd {cron_root(self.root)} && "
                 f"/usr/bin/uv run --script "
                 f"{cron_root(self.root / 'old' / 'run.py')} "
                 "--name smoke-fixture --quiet 2>&1")
        plan = migrate.plan_migration([stale])
        self.assertIn("smoke-fixture", plan["schedule"])

    def test_legacy_watcher_line_is_planned_for_internal_rewrite(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        root = cron_root(self.root)
        stale = (
            f"@reboot cd {root} && agents-live --repo {root} "
            "start --ensure-watcher smoke-fixture 2>&1"
        )
        plan = migrate.plan_migration([stale])
        old, new = plan["watcher"]["smoke-fixture"]
        self.assertEqual(old, [stale])
        self.assertIn("internal ensure-watcher smoke-fixture", new[0])

    def test_undefined_agent_is_reported_not_planned(self) -> None:
        line = (f"{TEST_CRON_SCHEDULE} cd {cron_root(self.root)} && "
                "uv run --script x.py --name ghost-agent --quiet 2>&1")
        plan = migrate.plan_migration([line])
        self.assertEqual(plan["schedule"], {})
        self.assertIn("ghost-agent", plan["missing"])

    def test_foreign_same_named_entries_are_not_migrated(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        foreign = (f"{TEST_CRON_SCHEDULE} cd {FOREIGN_REPO} && "
                   f"agents-live --repo {FOREIGN_REPO} run "
                   "--name smoke-fixture --quiet 2>&1")
        plan = migrate.plan_migration([foreign])
        self.assertEqual(plan, {"schedule": {}, "watcher": {}, "missing": []})

    def test_adopt_rewrites_defined_schedule_and_watcher_only(self) -> None:
        self.write_agent(
            "smoke-fixture",
            AGENT_DEFINITION.replace(
                f'schedule: "{TEST_CRON_SCHEDULE}"',
                f'schedule: "{TEST_CRON_SCHEDULE}"\nwatchPath: inbox',
            ),
        )
        old_root = self.root / "moved-project"
        old = cron_root(old_root)
        old_schedule = (
            f"{TEST_CRON_SCHEDULE} cd {old} && agents-live "
            f"--repo {old} run --name smoke-fixture --quiet 2>&1")
        old_watcher = (
            f"@reboot cd {old} && agents-live --repo {old} "
            "internal ensure-watcher smoke-fixture 2>&1")
        undefined = (
            f"{TEST_CRON_SCHEDULE} cd {old} && agents-live "
            f"--repo {old} run --name missing-agent --quiet 2>&1")
        near_match = old_schedule.replace(str(old_root), f"{old_root}-other")
        mixed_live = old_schedule.replace(
            f"--repo {old}", f"--repo {cron_root(self.root)}")
        live_entry = schedule_lines("smoke-fixture")[0]
        lines = [
            old_schedule, old_watcher, undefined, near_match, mixed_live,
            live_entry,
        ]

        plan = migrate.plan_adoption(lines, old_root)
        self.assertEqual(plan["schedule"]["smoke-fixture"][0], [old_schedule])
        self.assertEqual(plan["watcher"]["smoke-fixture"][0], [old_watcher])
        self.assertEqual(plan["unmatched"], [undefined])

        rewritten = migrate._apply_adoption(lines, plan)
        self.assertNotIn(old_schedule, rewritten)
        self.assertNotIn(old_watcher, rewritten)
        self.assertIn(undefined, rewritten)
        self.assertIn(near_match, rewritten)
        self.assertIn(mixed_live, rewritten)
        self.assertIn(live_entry, rewritten)

    def test_adopt_dry_run_and_install_use_safe_paths(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        old_root = self.root / "moved-project"
        old_line = (
            f"{TEST_CRON_SCHEDULE} cd {cron_root(old_root)} && agents-live "
            f"--repo {cron_root(old_root)} run "
            "--name smoke-fixture --quiet 2>&1")
        with (
            mock.patch.object(
                headless, "current_crontab_lines", return_value=[old_line]),
            mock.patch.object(headless, "install_crontab") as install,
            mock.patch("sys.argv", ["agents-live migrate", "--adopt",
                                    str(old_root), "--dry-run"]),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(migrate.main(), 0)
        install.assert_not_called()

        with (
            mock.patch.object(
                headless, "current_crontab_lines", return_value=[old_line]),
            mock.patch.object(
                headless, "crontab_lock",
                return_value=contextlib.nullcontext()) as lock,
            mock.patch.object(headless, "install_crontab") as install,
            mock.patch("sys.argv", ["agents-live migrate", "--adopt",
                                    str(old_root)]),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(migrate.main(), 0)
        lock.assert_called_once()
        install.assert_called_once()

    def test_adopt_rejects_an_existing_old_root(self) -> None:
        with (
            mock.patch("sys.argv", ["agents-live migrate", "--adopt",
                                    str(self.root)]),
            self.assertRaisesRegex(headless.AgentsLiveError, "still exists"),
        ):
            migrate.main()

    def test_health_check_ignores_foreign_watcher_entries(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        foreign_repo = f"{self.root}-foreign"
        # The foreign project exists on disk: its entries are its own
        # business (only lines from MISSING roots are ever flagged).
        Path(foreign_repo).mkdir()
        self.addCleanup(shutil.rmtree, foreign_repo, ignore_errors=True)
        crontab = "\n".join([
            f"@reboot cd {cron_root(foreign_repo)} && "
            f"agents-live --repo {cron_root(foreign_repo)} "
            "start --ensure-watcher missing",
            f"@reboot cd {cron_root(self.root)} && "
            f"agents-live --repo {cron_root(self.root)} "
            "internal ensure-watcher smoke-fixture",
        ])
        completed = subprocess.CompletedProcess(
            ["crontab", "-l"], 0, stdout=crontab, stderr="")
        with (
            mock.patch.object(doctor, "REPO", self.root),
            mock.patch.object(doctor.subprocess, "run", return_value=completed),
        ):
            self.assertEqual(doctor._crontab_inconsistencies(), ([], []))


class _FakeHostBinaries(_TempProject):
    """Base for tests that drive the real POSIX dispatch mechanisms.

    The rest of the suite patches ``current_crontab_lines``,
    ``install_crontab``, and never starts ``inotifywait`` - exactly the
    subsystems a host-runtime seam replaces. These tests instead put a
    fake executable on PATH and assert on observable outcomes (the
    resulting table, the dispatched batch), so they keep holding when
    the mechanism behind them changes.
    """

    def setUp(self) -> None:
        super().setUp()
        self.bin_dir = self.root / "fake-bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self._saved_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{self._saved_path}"
        self.addCleanup(os.environ.__setitem__, "PATH", self._saved_path)

    def write_executable(self, name: str, body: str) -> Path:
        path = self.bin_dir / name
        path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        path.chmod(0o755)
        return path


@unittest.skipIf(
    sys.platform == "win32",
    "drives a real crontab process, and Windows has none: a shebang "
    "script is not something CreateProcess will run. The Task Scheduler "
    "branch this host actually dispatches through is covered by "
    "TestWindowsScheduling.")
class TestCrontabConvergenceBehavior(_FakeHostBinaries):
    """Install, converge, and remove against a real ``crontab`` process."""

    USER_LINE = "MAILTO=nobody"

    def setUp(self) -> None:
        super().setUp()
        self.table = self.root / "crontab.txt"
        self.write_executable("crontab", f"""
import sys
from pathlib import Path

table = Path({str(self.table)!r})
flag = sys.argv[1] if len(sys.argv) > 1 else ""
if flag == "-l":
    if not table.exists():
        sys.stderr.write("no crontab for tester\\n")
        raise SystemExit(1)
    sys.stdout.write(table.read_text(encoding="utf-8"))
elif flag == "-":
    table.write_text(sys.stdin.read(), encoding="utf-8")
elif flag == "-r":
    table.unlink(missing_ok=True)
else:
    sys.stderr.write(f"unsupported crontab flag: {{flag}}\\n")
    raise SystemExit(2)
""")
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        (self.root / "Agents" / "handlers").mkdir(parents=True, exist_ok=True)
        (self.root / "Agents" / "handlers" / "prep.py").write_text(
            "print('{}')\n", encoding="utf-8")
        self.foreign_line = (
            f"{TEST_CRON_SCHEDULE} cd {FOREIGN_REPO} && agents-live "
            f"--repo {FOREIGN_REPO} run --name smoke-fixture --quiet 2>&1")

    def installed_lines(self) -> list[str]:
        if not self.table.exists():
            return []
        return [l for l in self.table.read_text(encoding="utf-8").splitlines() if l]

    def seed(self, lines: list[str]) -> None:
        self.table.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_repeated_installs_converge_on_one_entry(self) -> None:
        self.seed([self.USER_LINE, self.foreign_line])
        canonical = schedule_lines("smoke-fixture")

        activate.install_cron_agent("smoke-fixture")
        after_first = self.installed_lines()
        activate.install_cron_agent("smoke-fixture")
        after_second = self.installed_lines()

        self.assertEqual(after_first, after_second)
        self.assertEqual(
            [l for l in after_second if l in canonical], canonical)
        # Unrelated user content and another project's entry for the same
        # agent name both survive an install here.
        self.assertIn(self.USER_LINE, after_second)
        self.assertIn(self.foreign_line, after_second)

    def test_stale_entry_is_migrated_to_the_canonical_form(self) -> None:
        stale = (f"{TEST_CRON_SCHEDULE} cd {cron_root(self.root)} && "
                 f"/usr/bin/uv run --script "
                 f"{cron_root(self.root / 'scripts' / 'run.py')} "
                 "--name smoke-fixture --quiet 2>&1")
        self.seed([self.USER_LINE, stale, self.foreign_line])
        canonical = schedule_lines("smoke-fixture")
        self.assertNotIn(stale, canonical)

        plan = migrate.plan_migration(self.installed_lines())
        self.assertEqual(plan["schedule"]["smoke-fixture"], ([stale], canonical))

        with (
            mock.patch("sys.argv", ["agents-live migrate"]),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(migrate.main(), 0)

        converged = self.installed_lines()
        self.assertNotIn(stale, converged)
        for line in canonical:
            self.assertIn(line, converged)
        self.assertIn(self.USER_LINE, converged)
        self.assertIn(self.foreign_line, converged)

        # Converged input is a no-op: the second pass plans nothing.
        self.assertEqual(migrate.plan_migration(converged)["schedule"], {})

    def test_removal_takes_only_this_repos_entries(self) -> None:
        self.seed([self.USER_LINE, self.foreign_line])
        activate.install_cron_agent("smoke-fixture")
        headless.install_watcher_reboot_line("smoke-fixture")
        self.assertTrue(headless.remove_cron_entries("smoke-fixture"))
        self.assertTrue(headless.remove_watcher_reboot_line("smoke-fixture"))
        self.assertEqual(
            self.installed_lines(), [self.USER_LINE, self.foreign_line])

    def test_unreadable_crontab_never_rewrites_the_table(self) -> None:
        self.seed([self.USER_LINE, self.foreign_line])
        self.write_executable("crontab", """
import sys

sys.stderr.write("crontab: not allowed here\\n")
raise SystemExit(1)
""")
        with self.assertRaisesRegex(headless.AgentsLiveError, "not accessible"):
            activate.install_cron_agent("smoke-fixture")
        self.assertEqual(
            self.installed_lines(), [self.USER_LINE, self.foreign_line])


WATCHER_DEFINITION = """---
description: Smoke fixture. Never delegate to this agent.
disable-model-invocation: true
runtime: none
mode: plan
watchPath: .
watchIgnore:
  - skip.md
  - "Agents/data/"
---
Watcher smoke fixture body.
"""


class _ScriptedEventSource:
    """One batch of absolute paths, then the end of the watch.

    The loop treats ``WatchFailed`` as "the watch ended": it drains
    whatever it has and returns. That is exactly what a scripted
    ``inotifywait`` that exits used to produce, minus the dependency on
    a platform's watch mechanism - Windows never runs ``inotifywait``,
    so a fake one there left the loop blocked on a real directory.
    """

    def __init__(self, paths) -> None:
        self._paths = list(paths)

    def start(self) -> None:
        return None

    def poll(self, timeout: float | None) -> list[str]:
        if not self._paths:
            raise watchsource.WatchFailed("watch ended")
        batch, self._paths = self._paths, []
        return batch

    def stop(self) -> None:
        return None


class TestWatchLoopBehavior(_FakeHostBinaries):
    """Drive ``watch_loop`` end to end against a scripted event source."""

    def setUp(self) -> None:
        super().setUp()
        # Only to satisfy the Linux prerequisite check; events come from
        # the scripted source, not from this program.
        self.write_executable("inotifywait", "pass\n")
        # The loop installs handlers for whichever of these the host
        # has; SIGHUP has no Windows spelling.
        self._saved_handlers = {
            sig: signal.getsignal(sig)
            for sig in (getattr(signal, name, None)
                        for name in ("SIGTERM", "SIGINT", "SIGHUP"))
            if sig is not None
        }
        self.addCleanup(self._restore_handlers)

    def _restore_handlers(self) -> None:
        for sig, handler in self._saved_handlers.items():
            signal.signal(sig, handler)

    def write_repo_file(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def run_watch_loop(self, name: str, changed: list[str]) -> list[list[str]]:
        """Feed *changed* to the loop; return the dispatched batches."""
        events = [str(self.root / relative) for relative in changed]
        batches: list[list[str]] = []
        with (
            mock.patch.object(
                watchsource, "open_source",
                side_effect=lambda directories, *, cwd: _ScriptedEventSource(events)),
            mock.patch.object(activate, "_dispatch_run_once",
                              side_effect=lambda _n, files: batches.append(files)),
            # watch_loop registers exit hooks for the watcher process; in
            # a test they would fire against a deleted temp state home.
            mock.patch("atexit.register"),
        ):
            self.assertEqual(activate.watch_loop(name), 0)
        return batches

    def test_dispatch_carries_repo_relative_paths_and_drops_ignored_ones(self) -> None:
        self.write_agent("watch-fixture", WATCHER_DEFINITION)
        changed = [
            "notes/keep.md",          # dispatched
            "skip.md",                # watchIgnore entry
            "Agents/data/state.json",  # watchIgnore directory prefix
            "notes/_index_.md",       # generated index
            ".hidden/secret.md",      # dotted path component
            "notes/__pycache__/x.pyc",
        ]
        for relative in changed:
            self.write_repo_file(relative, f"content of {relative}\n")

        self.assertEqual(self.run_watch_loop("watch-fixture", changed),
                         [["notes/keep.md"]])

    def test_unchanged_content_is_filtered_inside_the_cascade_window(self) -> None:
        self.write_agent("watch-fixture", WATCHER_DEFINITION)
        self.write_repo_file("notes/keep.md", "first\n")
        self.assertEqual(self.run_watch_loop("watch-fixture", ["notes/keep.md"]),
                         [["notes/keep.md"]])

        # Same content again: a cascade re-touch, not an edit.
        self.assertEqual(self.run_watch_loop("watch-fixture", ["notes/keep.md"]),
                         [])

        self.write_repo_file("notes/keep.md", "second\n")
        self.assertEqual(self.run_watch_loop("watch-fixture", ["notes/keep.md"]),
                         [["notes/keep.md"]])

    def test_debounced_batch_survives_watcher_exit(self) -> None:
        self.write_agent(
            "watch-fixture",
            WATCHER_DEFINITION.replace("---\nWatcher", "debounce: 30\n---\nWatcher"))
        self.write_repo_file("notes/keep.md", "first\n")
        self.write_repo_file("notes/other.md", "other\n")

        # A 30s quiet window never elapses here; the pending batch must
        # still dispatch once when the watcher process goes away.
        self.assertEqual(
            self.run_watch_loop("watch-fixture", ["notes/keep.md", "notes/other.md"]),
            [["notes/keep.md", "notes/other.md"]])

    def test_file_target_watch_filters_by_filename(self) -> None:
        self.write_agent("watch-fixture", WATCHER_DEFINITION.replace(
            "watchPath: .", "watchPath: notes/keep.md"))
        self.write_repo_file("notes/keep.md", "first\n")
        self.write_repo_file("notes/other.md", "other\n")

        self.assertEqual(
            self.run_watch_loop("watch-fixture", ["notes/other.md", "notes/keep.md"]),
            [["notes/keep.md"]])


class TestWatchPolicy(unittest.TestCase):
    """The watcher rules, exercised without an event source."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_batch_within_the_limit_is_carried_whole(self) -> None:
        paths = [f"note{index}.md" for index in range(4)]
        self.assertEqual(watchpolicy.bound_batch(paths, limit=4), (paths, 0))

    def test_a_batch_past_the_limit_is_cut_and_counted(self) -> None:
        # A rescan can select thousands of files. The agent gets a list
        # it can act on; the count is what the log has to say about the
        # rest, because a silent truncation is a lie about the batch.
        paths = [f"note{index}.md" for index in range(10)]
        carried, omitted = watchpolicy.bound_batch(paths, limit=4)
        self.assertEqual(carried, paths[:4])
        self.assertEqual(omitted, 6)

    def select(self, paths: list[str], **kwargs: object) -> list[str]:
        return watchpolicy.select_batch(
            [str(self.root / p) for p in paths], root=self.root, **kwargs)

    def test_generated_and_hidden_paths_never_reach_an_agent(self) -> None:
        self.assertEqual(
            self.select([
                "notes/keep.md", ".git/index", "notes/_index_.md",
                "lib/__pycache__/x.pyc", "Agents/logs/note-index.log",
            ]),
            ["notes/keep.md"])

    def test_ignore_entries_match_names_and_directory_prefixes(self) -> None:
        self.assertEqual(
            self.select(
                ["notes/keep.md", "notes/skip.md", "Agents/data/state.json"],
                watch_ignore=["skip.md", "Agents/data/"]),
            ["notes/keep.md"])

    def test_repeated_events_collapse_to_one_entry(self) -> None:
        self.assertEqual(
            self.select(["notes/keep.md", "notes/keep.md"]),
            ["notes/keep.md"])

    def test_file_targets_admit_their_own_name_and_directory_targets(self) -> None:
        self.assertEqual(
            self.select(
                ["notes/keep.md", "notes/other.md", "watched/deep/new.md"],
                target_filenames=frozenset({"keep.md"}),
                dir_targets=[self.root / "watched"]),
            ["notes/keep.md", "watched/deep/new.md"])

    def test_paths_outside_the_repo_stay_absolute(self) -> None:
        outside = "/elsewhere/file.md"
        self.assertEqual(
            watchpolicy.select_batch([outside], root=self.root), [outside])

    def test_unchanged_files_are_dropped_inside_the_cascade_window(self) -> None:
        decision = watchpolicy.apply_cascade_guard(
            ["same.md", "edited.md"],
            cached_hashes={"same.md": "aaa", "edited.md": "bbb"},
            last_dispatch_at=1000.0, now=1001.0,
            hasher=lambda f: {"same.md": "aaa", "edited.md": "ccc"}[f])
        self.assertEqual(decision.dispatch, ["edited.md"])
        self.assertEqual(decision.skipped, ["same.md"])
        self.assertEqual(decision.hashes["edited.md"], "ccc")

    def test_outside_the_window_an_unchanged_file_still_dispatches(self) -> None:
        decision = watchpolicy.apply_cascade_guard(
            ["same.md"], cached_hashes={"same.md": "aaa"},
            last_dispatch_at=1000.0,
            now=1000.0 + watchpolicy.CASCADE_WINDOW_SECS + 1,
            hasher=lambda _f: "aaa")
        self.assertEqual(decision.dispatch, ["same.md"])
        self.assertEqual(decision.skipped, [])

    def test_a_file_that_cannot_be_hashed_stays_in_the_batch(self) -> None:
        decision = watchpolicy.apply_cascade_guard(
            ["deleted.md"], cached_hashes={"deleted.md": "aaa"},
            last_dispatch_at=1000.0, now=1001.0, hasher=lambda _f: None)
        self.assertEqual(decision.dispatch, ["deleted.md"])
        self.assertEqual(decision.hashes, {})

    def test_breaker_trips_only_past_the_cap(self) -> None:
        breaker = watchpolicy.FireRateBreaker(window_secs=60,
                                              max_dispatches=3)
        self.assertEqual(
            [breaker.record(float(n)) for n in range(4)],
            [False, False, False, True])

    def test_breaker_forgets_dispatches_older_than_the_window(self) -> None:
        breaker = watchpolicy.FireRateBreaker(window_secs=60,
                                              max_dispatches=3)
        for second in range(4):
            breaker.record(float(second))
        self.assertFalse(breaker.record(1000.0))
        self.assertEqual(breaker.count, 1)

    def test_debounce_merges_batches_and_restarts_the_window(self) -> None:
        window = watchpolicy.DebounceWindow(10.0)
        self.assertIsNone(window.remaining(0.0))
        window.add(["a.md"], 0.0)
        window.add(["a.md", "b.md"], 5.0)
        self.assertEqual(window.remaining(5.0), 10.0)
        self.assertEqual(window.take(), ["a.md", "b.md"])
        self.assertEqual(window.take(), [])
        self.assertIsNone(window.remaining(5.0))


def _change_records(names: list[str]) -> bytes:
    """A ``FILE_NOTIFY_INFORMATION`` chain as the kernel would fill one."""
    chunks: list[bytes] = []
    for index, name in enumerate(names):
        encoded = name.encode("utf-16-le")
        # Records are 4-byte aligned; the last one says "no next entry".
        size = 12 + len(encoded)
        padding = (-size) % 4
        last = index == len(names) - 1
        chunks.append(struct.pack("<III", 0 if last else size + padding,
                                  1, len(encoded))
                      + encoded + b"\x00" * padding)
    return b"".join(chunks)


class TestWindowsWatchSource(unittest.TestCase):
    """The Windows event source, minus the kernel calls.

    ``ReadDirectoryChangesW`` itself cannot be exercised off Windows,
    but everything the watcher's correctness rests on can be: reading
    the records the kernel hands back, and what the source does with
    the two states it cannot get more information about - an overflowed
    buffer and a root that stopped being readable.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def buffer_of(self, names: list[str]) -> object:
        raw = _change_records(names)
        return ctypes.create_string_buffer(raw, len(raw))

    def test_a_filled_buffer_reads_back_as_the_names_that_changed(self) -> None:
        self.assertEqual(
            winwatch._records(self.buffer_of(["note.md", r"nested\deep.md"])),
            ["note.md", r"nested\deep.md"])

    def test_a_record_running_past_the_buffer_ends_the_chain(self) -> None:
        raw = bytearray(_change_records(["note.md", "second.md"]))
        # Claim a name far longer than the buffer holds: a truncated or
        # corrupt chain must stop the walk, not read past its end.
        struct.pack_into("<I", raw, 8, 4096)
        buffer = ctypes.create_string_buffer(bytes(raw), len(raw))
        self.assertEqual(winwatch._records(buffer), [])

    def test_an_overflowed_buffer_degrades_to_a_rescan(self) -> None:
        (self.root / "one.md").write_text("1", encoding="utf-8")
        (self.root / "nested").mkdir()
        (self.root / "nested" / "two.md").write_text("2", encoding="utf-8")
        source = winwatch.WindowsEventSource([self.root])

        source.events.put(("overflow", str(self.root)))

        # What changed is unrecoverable, so the source answers with a
        # superset: every file under the watched root.
        self.assertEqual(
            sorted(Path(p).name for p in source.poll(0.1)),
            ["one.md", "two.md"])
        # And the rescan happens once, not on every later poll.
        self.assertEqual(source.poll(0.01), [])

    def test_a_rescan_stays_bounded_however_large_the_storm(self) -> None:
        for index in range(12):
            (self.root / f"file{index}.md").write_text("x", encoding="utf-8")
        with mock.patch.object(winwatch, "RESCAN_FILE_LIMIT", 5):
            self.assertEqual(len(winwatch.rescan([self.root])), 5)

    def test_a_queue_that_is_full_drops_rather_than_blocking(self) -> None:
        # A reader that blocks stops calling ReadDirectoryChangesW, and
        # records that arrive with no read pending are lost anyway. The
        # drop has to be recorded instead.
        sink: queue.Queue = queue.Queue(maxsize=1)
        watch = winwatch.DirectoryWatch(self.root, sink)
        watch._offer("path", str(self.root / "one.md"))
        self.assertFalse(watch.dropped.is_set())
        watch._offer("path", str(self.root / "two.md"))
        self.assertTrue(watch.dropped.is_set())
        self.assertEqual(sink.qsize(), 1)

    def test_a_dropped_event_degrades_to_the_same_rescan(self) -> None:
        (self.root / "one.md").write_text("1", encoding="utf-8")
        (self.root / "two.md").write_text("2", encoding="utf-8")
        source = winwatch.WindowsEventSource([self.root])
        watch = source._watches[0]
        watch.dropped.set()

        # Nothing is known about what was dropped, so the answer is the
        # superset a buffer overflow would have given - and it is given
        # even though no event was waiting to wake the poll.
        self.assertEqual(sorted(Path(p).name for p in source.poll(0.1)),
                         ["one.md", "two.md"])
        self.assertFalse(watch.dropped.is_set())
        self.assertEqual(source.poll(0.01), [])

    def test_a_stop_does_not_wait_for_room_in_a_full_queue(self) -> None:
        source = winwatch.WindowsEventSource([])
        for index in range(winwatch.QUEUE_LIMIT):
            source.events.put_nowait(("path", f"file{index}.md"))
        # A poll woken by the sentinel is a poll with nothing else to
        # return; a full queue has plenty, so the stop must not block.
        source.stop()

    def test_a_watch_that_failed_is_raised_rather_than_polled_again(self) -> None:
        source = winwatch.WindowsEventSource([self.root])
        source.events.put(("failed", "the watched directory is no longer readable"))
        with self.assertRaises(winwatch.WatchFailed):
            source.poll(0.1)

    def test_a_poll_returns_the_whole_batch_that_is_waiting(self) -> None:
        source = winwatch.WindowsEventSource([self.root])
        for name in ("a.md", "b.md"):
            source.events.put(("path", str(self.root / name)))
        self.assertEqual([Path(p).name for p in source.poll(0.1)],
                         ["a.md", "b.md"])
        self.assertEqual(source.poll(0.01), [])

    def test_stopping_wakes_a_poll_that_is_waiting_without_a_timeout(self) -> None:
        source = winwatch.WindowsEventSource([])
        source.stop()
        # No timeout: without the stop sentinel this would never return.
        self.assertEqual(source.poll(None), [])


class TestWatchSourceSelection(unittest.TestCase):
    """Which mechanism a host watches files with."""

    def test_a_windows_host_watches_with_the_directory_change_api(self) -> None:
        with mock.patch.object(hostruntime, "id",
                               return_value=hostruntime.WINDOWS):
            self.assertEqual(watchsource.mechanism(), "ReadDirectoryChangesW")

    def test_every_other_host_keeps_inotifywait(self) -> None:
        with mock.patch.object(hostruntime, "id",
                               return_value=hostruntime.LINUX):
            self.assertEqual(watchsource.mechanism(), "inotifywait")


class TestTriggerSpecs(unittest.TestCase):
    """The vocabulary both trigger kinds share."""

    def spec(self, *, name: str = "agent", kind: str = triggers.SCHEDULE,
             root: str = "/repo", schedules: tuple[str, ...] = ("0 * * * *",),
             command: tuple[str, ...] = ("agents-live", "run", "--name",
                                         "agent"),
             ) -> triggers.TriggerSpec:
        return triggers.TriggerSpec(
            name=name, kind=kind, root=Path(root), schedules=schedules,
            command=command, path="/usr/bin")

    def test_one_line_per_schedule_carries_root_and_path(self) -> None:
        spec = self.spec(schedules=("@reboot", "0 * * * *"))
        lines = triggers.render(spec)
        self.assertEqual(len(lines), 2)
        # cron_root, not the literal "/repo": the assertion is about the
        # shape of the line, and the host decides how a root is spelled.
        prefix = f"@reboot cd {cron_root(spec.root)} && PATH=/usr/bin "
        self.assertTrue(lines[0].startswith(prefix))
        self.assertTrue(all(line.endswith("2>&1") for line in lines))

    def test_a_spec_matches_the_lines_it_renders(self) -> None:
        spec = self.spec()
        self.assertTrue(all(
            triggers.matches(line, root=spec.root, name=spec.name,
                             kind=spec.kind)
            for line in triggers.render(spec)))

    def test_another_projects_line_is_never_this_projects_trigger(self) -> None:
        line = triggers.render(self.spec(root="/other"))[0]
        self.assertFalse(triggers.matches(line, root=Path("/repo"),
                                          name="agent",
                                          kind=triggers.SCHEDULE))

    def test_a_watcher_line_is_not_a_schedule_line(self) -> None:
        watcher = triggers.render(self.spec(
            kind=triggers.WATCHER, schedules=("@reboot",),
            command=("agents-live", "internal", "ensure-watcher", "agent")))[0]
        self.assertTrue(triggers.matches(watcher, root=Path("/repo"),
                                         name="agent", kind=triggers.WATCHER))
        self.assertFalse(triggers.matches(watcher, root=Path("/repo"),
                                          name="agent",
                                          kind=triggers.SCHEDULE))

    def test_names_that_are_substrings_stay_distinct(self) -> None:
        line = triggers.render(self.spec(
            command=("agents-live", "run", "--name", "todo")))[0]
        self.assertTrue(triggers.matches(line, root=Path("/repo"),
                                         name="todo",
                                         kind=triggers.SCHEDULE))
        self.assertFalse(triggers.matches(line, root=Path("/repo"),
                                          name="todo-push",
                                          kind=triggers.SCHEDULE))

    def test_an_unrelated_entry_in_the_repo_names_no_agent(self) -> None:
        self.assertIsNone(triggers.agent_name(
            "0 * * * * cd /repo && ./tidy.sh --name agent",
            root=Path("/repo"), kind=triggers.SCHEDULE))

    def test_canonical_ignores_order_but_not_content(self) -> None:
        spec = self.spec(schedules=("@reboot", "0 * * * *"))
        rendered = triggers.render(spec)
        self.assertTrue(triggers.is_canonical(list(reversed(rendered)), spec))
        self.assertFalse(triggers.is_canonical(rendered[:1], spec))
        self.assertFalse(triggers.is_canonical(
            [line.replace("/usr/bin", "/opt/bin") for line in rendered], spec))


class TestAdapterRegistry(unittest.TestCase):
    def test_public_adapters_present(self) -> None:
        self.assertEqual(agent_adapters.get("claude").family, "claude")
        self.assertEqual(agent_adapters.get("copilot").family, "copilot")

    def test_unknown_agent_fails_closed(self) -> None:
        with self.assertRaises(agent_adapters.UnknownRuntimeError):
            agent_adapters.get("no-such-agent")

    def test_registration_validates_fields(self) -> None:
        with self.assertRaises(ValueError):
            agent_adapters.register(agent_adapters.AgentAdapter(
                name="bad", binary=("bad",), family="no-such-family"))

    def test_identical_reregistration_tolerated_conflict_rejected(self) -> None:
        existing = agent_adapters.get("claude")
        agent_adapters.register(existing)  # no-op, no raise
        with self.assertRaises(ValueError):
            agent_adapters.register(agent_adapters.AgentAdapter(
                name="claude", binary=("elsewhere",), family="claude"))


class TestCliContract(_TempProject):
    @staticmethod
    def _valid_args(command: str) -> list[str]:
        return {
            "run": ["fixture"],
            "start": ["fixture"],
            "stop": ["fixture"],
            "internal": ["list-reboot-watchers"],
            "repos": ["list"],
            "completions": ["bash"],
        }.get(command, [])

    def setUp(self) -> None:
        super().setUp()
        saved_json = os.environ.pop(preflight.JSON_ENV_VAR, None)
        self.addCleanup(os.environ.pop, preflight.JSON_ENV_VAR, None)
        if saved_json is not None:
            self.addCleanup(
                os.environ.__setitem__, preflight.JSON_ENV_VAR, saved_json)
        for patcher in (
            mock.patch.object(update_check, "consume_notice", return_value=None),
            mock.patch.object(update_check, "launch_if_stale"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_help_entry_points(self) -> None:
        cases = (
            ([], "usage: agents-live", "--version"),
            (["--help"], "usage: agents-live", "upgrade"),
            (["help"], "usage: agents-live", "commands:"),
            (["help", "upgrade"], "usage: agents-live upgrade", "--skills-only"),
            (["upgrade", "--help"], "usage: agents-live upgrade", "--runtime-only"),
            (["upgrade", "help"], "usage: agents-live upgrade", "--runtime-only"),
        )
        for argv, usage, detail in cases:
            with (
                self.subTest(argv=argv),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                self.assertEqual(cli.main(argv), 0)
                self.assertIn(usage, stdout.getvalue())
                self.assertIn(detail, stdout.getvalue())
                self.assertEqual(stderr.getvalue(), "")

    def test_start_surface_rejects_internal_plumbing(self) -> None:
        help_text = cli.command_help(
            next(command for command in COMMANDS if command.name == "start"))
        for plumbing in (
                "--watch-loop", "--ensure-watcher", "--list-reboot-watchers"):
            with self.subTest(plumbing=plumbing):
                self.assertNotIn(plumbing, help_text)
                with mock.patch("sys.stderr", new_callable=io.StringIO):
                    self.assertEqual(cli.main(["start", plumbing]), 2)
        self.assertNotIn("internal", cli._usage())

    def test_internal_ensure_watcher_dispatches(self) -> None:
        with (
            mock.patch.object(preflight, "check", return_value=None),
            mock.patch.object(activate, "activate_watcher",
                              return_value=123) as ensure,
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(
                cli.main(["internal", "ensure-watcher", "fixture"]), 0)
        ensure.assert_called_once_with("fixture")
        self.assertEqual(
            stdout.getvalue(), "Ensured watcher for 'fixture': pid 123\n")

    def test_each_command_help_comes_from_spec(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command.name):
                stdout = io.StringIO()
                with mock.patch("sys.stdout", stdout):
                    self.assertEqual(cli.main([command.name, "--help"]), 0)
                self.assertIn(command.summary, stdout.getvalue())
                self.assertIn("--json", stdout.getvalue())
                self.assertIn("-h, --help, help", stdout.getvalue())

    def test_timeline_help_uses_subcommand_spec(self) -> None:
        for argv in (
                ["logs", "timeline", "--help"],
                ["help", "logs", "timeline"]):
            with (
                self.subTest(argv=argv),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(cli.main(argv), 0)
                help_text = stdout.getvalue()
                self.assertIn("usage: agents-live logs timeline", help_text)
                self.assertIn("--last", help_text)
                self.assertIn("Start time (ISO-8601 UTC).", help_text)
                self.assertNotIn("--agent", help_text)
                self.assertNotIn("--until", help_text)

        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(
                cli.main(["logs", "timeline", "--agent", "fixture"]), 2)
        self.assertIn("unrecognized argument: --agent", stderr.getvalue())

    def test_child_validation_uses_child_constraints(self) -> None:
        child = Cmd(
            "child", "Child.", "child", "in-process",
            args=(Arg(("--left",), "Use left."),
                Arg(("--right",), "Use right.")),
            mutually_exclusive=(("--left", "--right"),),
            requires_one_of=("--left", "--right"),
        )
        parent = Cmd(
            "parent", "Parent.", "parent", "in-process",
            subcommands=(child,), subcommand_required=True,
        )
        self.assertEqual(
            validation_error(parent, ["child"]),
            "child requires --left, or --right",
        )
        self.assertEqual(
            validation_error(parent, ["child", "--left", "--right"]),
            "--left and --right are mutually exclusive",
        )
        self.assertIsNone(validation_error(parent, ["child", "--left"]))

    def test_all_help_covers_every_public_command(self) -> None:
        with mock.patch(
                "sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(cli.main(["help", "--all"]), 0)
        help_text = stdout.getvalue()
        for command in COMMANDS:
            if command.hidden:
                continue
            with self.subTest(command=command.name):
                start = help_text.index(
                    f"usage: agents-live {command.name}")
                end = help_text.find("\n\nusage: agents-live ", start)
                section = help_text[start:end if end >= 0 else None]
                self.assertIn("--json", section)
                self.assertIn("-h, --help, help", section)
                for child in command.subcommands:
                    if not child.hidden:
                        child_start = help_text.index(
                            f"usage: agents-live {command.name} {child.name}")
                        child_end = help_text.find(
                            "\n\nusage: agents-live ", child_start)
                        child_section = help_text[
                            child_start:child_end if child_end >= 0 else None]
                        self.assertIn("--json", child_section)
                        self.assertIn("-h, --help, help", child_section)

    def test_usage_uses_package_version_and_links_grammar(self) -> None:
        with mock.patch.object(cli, "__version__", "9.8.7"):
            usage = cli._usage()
        self.assertIn("/blob/v9.8.7/", usage)
        self.assertIn("commands.md#cli-grammar", usage)

    def test_generated_command_docs_have_not_drifted(self) -> None:
        commands_doc = (
            Path(headless.__file__).parent / "skill" / "docs" / "commands.md"
        ).read_text(encoding="utf-8")
        start = commands_doc.index("<!-- BEGIN GENERATED CLI -->")
        end_marker = "<!-- END GENERATED CLI -->"
        end = commands_doc.index(end_marker, start) + len(end_marker)
        self.assertEqual(commands_doc[start:end], render_docs_block())

    def test_each_command_rejects_unknown_flags(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command.name):
                stderr = io.StringIO()
                with mock.patch("sys.stderr", stderr):
                    self.assertEqual(
                        cli.main([command.name, "--contract-unknown"]), 2)
                self.assertIn("unrecognized argument", stderr.getvalue())

    def test_flag_spellings_argparse_accepts_pass_the_spec_gate(self) -> None:
        # The pre-dispatch gate must accept every spelling the target
        # module's argparse accepts: --flag=value and attached short
        # option values (-n20).
        run_cmd = cli.COMMAND_BY_NAME["run"]
        logs_cmd = cli.COMMAND_BY_NAME["logs"]
        self.assertIsNone(cli.validation_error(run_cmd, ["--name=fixture"]))
        self.assertIsNone(cli.unknown_flag(logs_cmd, ["-n20"]))
        self.assertIsNone(cli.unknown_flag(run_cmd, ["--name=fixture"]))
        self.assertEqual(cli.unknown_flag(logs_cmd, ["-x2"]), "-x2")
        self.assertEqual(
            cli.validation_error(run_cmd, []), "--name is required")

    def test_spec_constraints_are_declared_not_hardcoded(self) -> None:
        start = cli.COMMAND_BY_NAME["start"]
        repos_cmd = cli.COMMAND_BY_NAME["repos"]
        internal = cli.COMMAND_BY_NAME["internal"]
        self.assertEqual(
            cli.validation_error(start, ["--yes", "--all"]),
            "--yes and --all are mutually exclusive")
        self.assertEqual(
            cli.validation_error(start, []),
            "start requires NAME, --name NAME, or --all")
        self.assertIsNone(cli.validation_error(start, ["--name=fixture"]))
        self.assertEqual(
            cli.validation_error(repos_cmd, []),
            "repos requires one of: list, add, default, remove")
        for argv in (["watch-loop", "x"], ["ensure-watcher", "x"],
                     ["list-reboot-watchers"]):
            self.assertIsNone(cli.validation_error(internal, argv))
            self.assertIsNone(cli.unknown_flag(internal, argv))

    def test_records_shape_is_stable_for_zero_one_and_many_rows(self) -> None:
        # A records-shaped command (logs) must present one envelope
        # shape regardless of row count.
        for stdout_text, expected in (
            ("", []),
            ('{"a": 1}\n', [{"a": 1}]),
            ('{"a": 1}\n{"a": 2}\n', [{"a": 1}, {"a": 2}]),
        ):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                cli._captured_result(0, "logs", stdout_text, "",
                                     shape="records")
            payload = json.loads(captured.getvalue())
            self.assertEqual(payload["records"], expected)
            self.assertTrue(payload["ok"])

    def test_all_repos_capability_follows_spec(self) -> None:
        for command in COMMANDS:
            if command.all_repos:
                continue
            with self.subTest(command=command.name):
                stderr = io.StringIO()
                with mock.patch("sys.stderr", stderr):
                    self.assertEqual(
                        cli.main([command.name, "--all-repos"]), 2)
                self.assertIn("does not support --all-repos", stderr.getvalue())

    def test_root_none_commands_do_not_resolve_a_project(self) -> None:
        fake_module = mock.Mock()
        fake_module.main.return_value = 0
        for command in COMMANDS:
            if command.root != "none":
                continue
            with (
                self.subTest(command=command.name),
                mock.patch("importlib.import_module",
                           return_value=fake_module),
                mock.patch.object(paths, "resolve_root") as resolve_root,
            ):
                self.assertEqual(
                    cli.main([command.name, *self._valid_args(command.name)]),
                    0)
                resolve_root.assert_not_called()

    def test_only_read_only_commands_may_use_the_registry_fallback(
            self) -> None:
        """The seam that keeps issue #192 from coming back.

        ``root="registry"`` lets a command resolve to the sole registered
        repository when nothing else names one. That is safe only for
        commands that read; anything that writes triggers or state has to
        keep failing loudly rather than act on a project the invocation
        never named.
        """
        def walk(commands):
            for command in commands:
                yield command
                yield from walk(command.subcommands)

        self.assertEqual(
            {command.name for command in walk(COMMANDS)
             if command.root == "registry"},
            {"status", "logs", "timeline", "dashboard"})

    def test_required_root_commands_emit_no_project_envelope(self) -> None:
        saved_cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as outside, \
                    _cwd_restored_before_cleanup(saved_cwd):
                os.chdir(outside)
                environ = {
                    key: value for key, value in os.environ.items()
                    if key not in (paths.ENV_VAR, "AGENTS_LIVE_JSON")
                }
                environ["XDG_CONFIG_HOME"] = str(
                    Path(outside) / "isolated-config")
                with mock.patch.dict(os.environ, environ, clear=True):
                    for command in COMMANDS:
                        if command.root not in ("required", "registry"):
                            continue
                        with self.subTest(command=command.name):
                            paths.clear_cache()
                            stdout = io.StringIO()
                            stderr = io.StringIO()
                            argv = (
                                ["--json", command.name,
                                 *self._valid_args(command.name)]
                                if command.json else [
                                    command.name,
                                    *self._valid_args(command.name)]
                            )
                            with (
                                mock.patch("sys.stdout", stdout),
                                mock.patch("sys.stderr", stderr),
                            ):
                                self.assertEqual(
                                    cli.main(argv), 2)
                            if command.json:
                                envelope = json.loads(stdout.getvalue())
                                self.assertEqual(
                                    envelope["error"]["code"],
                                    "no_project_root")
                            else:
                                self.assertIn(
                                    "error [no_project_root]",
                                    stderr.getvalue())
                            os.environ.pop("AGENTS_LIVE_JSON", None)
        finally:
            os.chdir(saved_cwd)
            paths.clear_cache()

    def test_declared_aliases_dispatch_like_canonical_names(self) -> None:
        for command in COMMANDS:
            for alias in command.aliases:
                calls: list[list[str]] = []
                fake_module = mock.Mock()
                fake_module.main.side_effect = (
                    lambda: calls.append(sys.argv[1:]) or 0)
                with (
                    self.subTest(command=command.name, alias=alias),
                    mock.patch("importlib.import_module",
                               return_value=fake_module),
                    mock.patch.object(preflight, "check", return_value=None),
                ):
                    self.assertEqual(
                        cli.main([command.name, *self._valid_args(command.name)]),
                        0)
                    self.assertEqual(cli.main([alias]), 0)
                self.assertEqual(calls, [[], []])

    def test_json_commands_accept_both_flag_positions(self) -> None:
        fake_module = mock.Mock()
        fake_module.main.side_effect = lambda: print("human result") or 0
        completed = subprocess.CompletedProcess(
            [], 0, stdout='{"record": true}\n', stderr="")
        for command in COMMANDS:
            if not command.json:
                continue
            outputs = []
            suffix = self._valid_args(command.name)
            for argv in (["--json", command.name, *suffix],
                         [command.name, *suffix, "--json"]):
                stdout = io.StringIO()
                with (
                    self.subTest(command=command.name, argv=argv),
                    mock.patch("importlib.import_module",
                               return_value=fake_module),
                    mock.patch.object(cli.subprocess, "run",
                                      return_value=completed),
                    mock.patch.object(preflight, "check", return_value=None),
                    contextlib.redirect_stdout(stdout),
                ):
                    self.assertEqual(cli.main(argv), 0)
                    outputs.append(json.loads(stdout.getvalue()))
            self.assertEqual(outputs[0], outputs[1])

    def test_json_commands_emit_typed_failure_envelopes(self) -> None:
        fake_module = mock.Mock()
        typed_error = RuntimeError("contract failure")
        typed_error.category = "agent_error"
        fake_module.main.side_effect = typed_error
        completed = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="contract failure")
        for command in COMMANDS:
            if not command.json:
                continue
            stdout = io.StringIO()
            with (
                self.subTest(command=command.name),
                mock.patch("importlib.import_module",
                           return_value=fake_module),
                mock.patch.object(cli.subprocess, "run",
                                  return_value=completed),
                mock.patch.object(preflight, "check", return_value=None),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(cli.main([
                    "--json", command.name,
                    *self._valid_args(command.name),
                ]), 1)
                envelope = json.loads(stdout.getvalue())
                self.assertEqual(
                    envelope["error"]["operation"], command.name)
                self.assertIn("contract failure",
                              envelope["error"]["detail"])

    def test_nonzero_structured_result_passes_through_unwrapped(self) -> None:
        # A failing command's structured payload (doctor's
        # {ok: false, checks: [...]}, a FAIL verdict) is the result a
        # machine caller asked for; wrapping it in an operation_failed
        # envelope would destroy the detail exactly when it matters.
        fake_module = mock.Mock()
        fake_module.main.side_effect = (
            lambda: print('{"verdict": "FAIL"}') or 1)
        stdout = io.StringIO()
        with (
            mock.patch("importlib.import_module", return_value=fake_module),
            mock.patch.object(preflight, "check", return_value=None),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(cli.main(["run", "fixture", "--json"]), 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload, {"verdict": "FAIL"})

    def test_nonzero_unstructured_result_is_normalized_to_error_envelope(
            self) -> None:
        fake_module = mock.Mock()
        fake_module.main.side_effect = (
            lambda: print("plain text failure") or 1)
        stdout = io.StringIO()
        with (
            mock.patch("importlib.import_module", return_value=fake_module),
            mock.patch.object(preflight, "check", return_value=None),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(cli.main(["run", "fixture", "--json"]), 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["error"]["code"], "operation_failed")

    def test_uncategorized_exceptions_reraise_with_traceback(self) -> None:
        # Programming bugs must stay diagnosable: only typed errors
        # (carrying a category) are flattened into envelopes.
        fake_module = mock.Mock()
        fake_module.main.side_effect = KeyError("latent bug")
        with (
            mock.patch("importlib.import_module", return_value=fake_module),
            mock.patch.object(preflight, "check", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(KeyError):
                cli.main(["run", "fixture", "--json"])

    def test_json_argparse_exit_emits_usage_envelope(self) -> None:
        # A subcommand's own argparse rejecting argv inside the capture
        # must surface as an envelope, never an empty-output exit.
        stdout = io.StringIO()
        with (
            mock.patch.object(preflight, "check", return_value=None),
            contextlib.redirect_stdout(stdout),
        ):
            code = cli.main(["--json", "status", "fixture", "extra"])
        self.assertEqual(code, 2)
        envelope = json.loads(stdout.getvalue())
        self.assertEqual(envelope["error"]["code"], "usage_error")
        self.assertIn("unrecognized arguments", envelope["error"]["detail"])

    def test_json_logs_rejects_non_jsonl_format(self) -> None:
        # The capture envelope parses stdout as JSON lines; an explicit
        # non-jsonl --format would render a human table and silently
        # yield an empty-but-ok records envelope.
        stdout = io.StringIO()
        with (
            mock.patch.object(preflight, "check", return_value=None),
            contextlib.redirect_stdout(stdout),
        ):
            code = cli.main(["--json", "logs", "--format", "table"])
        self.assertEqual(code, 2)
        envelope = json.loads(stdout.getvalue())
        self.assertEqual(envelope["error"]["code"], "usage_error")
        self.assertIn("--format jsonl", envelope["error"]["detail"])

    def test_removed_duplicate_verbs_are_unknown(self) -> None:
        help_text = cli._usage()
        completion = completions.bash()
        for removed in ("teardown", "prereqs"):
            with (
                self.subTest(command=removed),
                mock.patch("sys.stderr",
                           new_callable=io.StringIO) as stderr,
            ):
                self.assertEqual(cli.main([removed]), 2)
                self.assertIn("[unknown_command]", stderr.getvalue())
            self.assertNotIn(removed, help_text)
            self.assertNotIn(removed, completion)

    def test_subprocess_dispatch_uses_declared_modules(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        with mock.patch.object(
                cli.subprocess, "run", return_value=completed) as run:
            self.assertEqual(cli.main(["logs", "--limit", "1"]), 0)
            self.assertEqual(cli.main(["logs", "timeline", "--last", "1"]), 0)
            self.assertEqual(cli.main(["dashboard", "--dev"]), 0)
        scripts = [Path(call.args[0][3]).name for call in run.call_args_list]
        self.assertEqual(scripts, ["qlog.py", "timeline.py", "dashboard.py"])

    def test_completion_scripts_follow_public_spec(self) -> None:
        scripts = {"bash": completions.bash(), "zsh": completions.zsh()}
        for shell, script in scripts.items():
            with self.subTest(shell=shell):
                for command in COMMANDS:
                    if command.hidden:
                        continue
                    self.assertIn(command.name, script)
                    for alias in command.aliases:
                        self.assertIn(alias, script)
                    for item in (command, *command.subcommands):
                        for argument in item.args:
                            for flag in argument.flags:
                                if flag.startswith("-") and not argument.hidden:
                                    self.assertIn(flag, script)
                    values = list(dict.fromkeys((
                        *(child.name for child in command.subcommands
                          if not child.hidden),
                        *(value
                          for item in (command, *command.subcommands)
                          if not item.hidden
                          for argument in visible_args(item)
                          for value in (*argument.flags, *argument.choices)
                          if value.startswith("-")
                          or value in argument.choices),
                        *(flag for argument in POST_COMMAND_ARGS
                          for flag in argument.flags),
                    )))
                    names = "|".join((command.name, *command.aliases))
                    expected_case = (
                        f"    {names}) opts={' '.join(values)!r} ;;"
                        if shell == "bash"
                        else f"    {names}) values=({' '.join(values)}) ;;"
                    )
                    self.assertIn(expected_case, script)
                self.assertIn("agents-live status --json", script)
                self.assertIn("-h", script)
                self.assertIn("--help", script)
                self.assertIn("help", script)
                self.assertIn("--all", script)
                self.assertNotIn("--watch-loop", script)
                self.assertNotIn("--ensure-watcher", script)

    @unittest.skipIf(
        sys.platform == "win32",
        "runs the completion script under bash, and a bare `bash` on a "
        "Windows PATH is as likely to be the WSL launcher as a shell. "
        "The artifact is a POSIX one; Linux CI is where it is checked.")
    def test_bash_completion_conforms_to_public_grammar(self) -> None:
        script = completions.bash()
        public = [command for command in COMMANDS if not command.hidden]

        def candidates(words: tuple[str, ...]) -> set[str]:
            quoted_words = " ".join(shlex.quote(word) for word in words)
            harness = (
                f"{script}\n"
                "_agents_live_agent_names() { :; }\n"
                f"COMP_WORDS=({quoted_words})\n"
                f"COMP_CWORD={len(words) - 1}\n"
                "_agents_live\n"
                "printf '%s\\n' \"${COMPREPLY[@]}\"\n"
            )
            completed = subprocess.run(
                ["bash"], input=harness, capture_output=True,
                text=True, check=True,
            )
            return set(completed.stdout.splitlines())

        top_level = candidates(("agents-live", ""))
        expected_top_level = {
            *(name for command in public
              for name in (command.name, *command.aliases)),
            *(flag for argument in (*GLOBAL_ARGS, HELP_ARG)
              for flag in argument.flags),
        }
        self.assertTrue(expected_top_level <= top_level)
        self.assertEqual(candidates(("agents-live", "hel")), {"help"})

        help_targets = candidates(("agents-live", "help", ""))
        self.assertTrue(
            {"--all", *(command.name for command in public)} <= help_targets)

        for command in public:
            expected = {
                *(child.name for child in command.subcommands
                  if not child.hidden),
                *(value
                  for item in (command, *command.subcommands)
                  if not item.hidden
                  for argument in visible_args(item)
                  for value in (*argument.flags, *argument.choices)
                  if value.startswith("-") or value in argument.choices),
                *(flag for argument in POST_COMMAND_ARGS
                  for flag in argument.flags),
            }
            with self.subTest(command=command.name):
                actual = candidates(("agents-live", command.name, ""))
                self.assertTrue(expected <= actual, expected - actual)

    def test_completions_command_prints_selected_shell(self) -> None:
        for shell, marker in (("bash", "complete -F"),
                              ("zsh", "#compdef agents-live")):
            with (
                self.subTest(shell=shell),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(cli.main(["completions", shell]), 0)
                self.assertIn(marker, stdout.getvalue())

    def test_completion_update_writes_both_xdg_scripts(self) -> None:
        bash_path = (
            self.root / "xdg-data" / "bash-completion" / "completions"
            / "agents-live")
        zsh_path = (
            self.root / "xdg-data" / "zsh" / "site-functions"
            / "_agents-live")
        bash_path.parent.mkdir(parents=True)
        bash_path.write_text("stale\n", encoding="utf-8")

        self.assertEqual(completions.update(), (bash_path, zsh_path))

        self.assertEqual(bash_path.read_text(encoding="utf-8"), completions.bash())
        self.assertEqual(zsh_path.read_text(encoding="utf-8"), completions.zsh())

    def test_completions_update_cli_reports_both_destinations(self) -> None:
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(cli.main(["completions", "--update"]), 0)
        output = stdout.getvalue()
        self.assertIn(str(completions.destinations()[0]), output)
        self.assertIn(str(completions.destinations()[1]), output)

    def test_completions_requires_one_mode(self) -> None:
        for argv in (["completions"],
                     ["completions", "bash", "--update"]):
            with (
                self.subTest(argv=argv),
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                self.assertEqual(cli.main(argv), 2)
                self.assertIn("error [usage_error]", stderr.getvalue())

    def test_explicit_completion_update_reports_write_failure(self) -> None:
        with (
            mock.patch.object(
                paths, "atomic_write_text", side_effect=PermissionError("denied")),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(cli.main(["completions", "--update"]), 1)
        self.assertIn("error [operation_failed] completions: denied",
                      stderr.getvalue())

    def test_completion_update_best_effort_warns(self) -> None:
        with (
            mock.patch.object(
                completions, "update", side_effect=PermissionError("denied")),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertFalse(completions.update_best_effort("init"))
        self.assertIn(
            "warning: could not update shell completions during init: denied",
            stderr.getvalue(),
        )

    def test_completion_remove_preserves_sibling_files(self) -> None:
        completions.update()
        sibling = completions.destinations()[0].with_name("other-command")
        sibling.write_text("keep\n", encoding="utf-8")

        self.assertEqual(completions.remove(), completions.destinations())

        self.assertFalse(completions.destinations()[0].exists())
        self.assertFalse(completions.destinations()[1].exists())
        self.assertTrue(sibling.is_file())

    def test_completions_help_explains_installation(self) -> None:
        for argv in (["completions", "help"], ["completions", "--help"],
                     ["help", "completions"]):
            with (
                self.subTest(argv=argv),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(cli.main(argv), 0)
                output = stdout.getvalue()
                self.assertIn("source <(agents-live completions bash)", output)
                self.assertIn("agents-live completions --update", output)
                self.assertIn("bash-completion", output)
                self.assertIn("fpath", output)

    def test_repos_list_exposes_structured_results(self) -> None:
        config_home = self.root / "contract-config"
        with mock.patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["repos", "list", "--json"]), 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertIn("repositories", payload)

    def test_maintenance_commands_are_not_public(self) -> None:
        for command in ("health-check", "migrate"):
            with self.subTest(command=command):
                self.assertNotIn(command, cli.COMMAND_BY_NAME)
                with mock.patch(
                        "sys.stderr", new_callable=io.StringIO) as stderr:
                    self.assertEqual(cli.main([command]), 2)
                self.assertIn("unknown command", stderr.getvalue())

    def test_version_works_outside_repository(self) -> None:
        saved = Path.cwd()
        selected_root = os.environ.pop(paths.ENV_VAR, None)
        paths.clear_cache()
        try:
            with tempfile.TemporaryDirectory() as outside, \
                    _cwd_restored_before_cleanup(saved):
                os.chdir(outside)
                with (
                    mock.patch.object(paths, "resolve_root") as resolve_root,
                    mock.patch.object(update_check, "interactive") as interactive,
                    mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
                ):
                    self.assertEqual(cli.main(["--version"]), 0)
                    # __version__ is THE version source (update checks,
                    # doctor); --version must read the same one.
                    self.assertEqual(
                        stdout.getvalue(),
                        f"agents-live {cli.__version__}\n",
                    )
                    resolve_root.assert_not_called()
                    interactive.assert_not_called()
        finally:
            os.chdir(saved)
            if selected_root is not None:
                os.environ[paths.ENV_VAR] = selected_root
            paths.clear_cache()

    def test_version_combines_with_other_global_flags(self) -> None:
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(cli.main(["--json", "--version"]), 0)
        self.assertIn(f"agents-live {cli.__version__}", stdout.getvalue())

    def test_init_repo_survives_global_flag_ordering(self) -> None:
        forms = (
            ["init", "--repo", str(self.root)],
            ["--json", "init", "--repo", str(self.root)],
            ["--json", "--repo", str(self.root), "init"],
            ["--repo", str(self.root), "--json", "init"],
        )
        for argv in forms:
            with (
                self.subTest(argv=argv),
                mock.patch.object(init, "main", return_value=0),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                os.environ.pop(cli.INIT_REPO_ENV_VAR, None)
                self.assertEqual(cli.main(argv), 0)
                self.assertEqual(
                    os.environ.get(cli.INIT_REPO_ENV_VAR), str(self.root))

    def test_heartbeat_works_outside_repository(self) -> None:
        os.environ.pop(paths.ENV_VAR, None)
        paths.clear_cache()
        with mock.patch.object(heartbeat, "run_once", return_value=0) as run:
            self.assertEqual(cli.main(["heartbeat"]), 0)
        run.assert_called_once_with()

    def test_unknown_command_exits_two(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
        ):
            self.assertEqual(cli.main(["frobnicate"]), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn(
            "error [unknown_command] frobnicate: unknown command 'frobnicate'",
            stderr.getvalue())

    def test_mutating_command_rejects_all_repos(self) -> None:
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            self.assertEqual(cli.main(["start", "--all-repos"]), 2)
        self.assertIn("select one repository", stderr.getvalue())

    def test_upgrade_dispatches_for_selected_project(self) -> None:
        stdout = io.StringIO()
        with (
            mock.patch.object(init, "install_skill", return_value=None) as install,
            mock.patch.object(
                health_check, "ensure_health_cron_lines", return_value=False),
            mock.patch.object(upgrade, "_migrate_triggers") as migrate,
            mock.patch("sys.stdout", stdout),
        ):
            self.assertEqual(cli.main(["upgrade", "--skills-only"]), 0)
        install.assert_called_once_with(self.root)
        migrate.assert_called_once_with(self.root)
        self.assertIn(
            "skill payload already matches the installed package",
            stdout.getvalue(),
        )

    def test_upgrade_works_outside_repository(self) -> None:
        os.environ.pop(paths.ENV_VAR, None)
        paths.clear_cache()
        with (
            mock.patch.object(upgrade, "_upgrade_runtime", return_value=0) as runtime,
            mock.patch.object(upgrade, "_targets", return_value=([], [])),
            mock.patch.object(
                upgrade, "_refresh_with_installed_cli", return_value=0) as refresh,
            mock.patch.object(paths, "resolve_root") as resolve_root,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(cli.main(["upgrade"]), 0)
        runtime.assert_called_once_with([], source=None)
        refresh.assert_called_once_with(refresh_skills=True)
        resolve_root.assert_not_called()

    def test_doctor_without_project_root_runs_host_checks(self) -> None:
        os.environ.pop(paths.ENV_VAR, None)
        paths.clear_cache()
        with (
            mock.patch.object(doctor, "REPO", None),
            mock.patch.object(doctor, "_has", return_value=True),
            mock.patch.object(doctor, "_python_312_resolvable", return_value=True),
            # The dispatch mechanisms are probed where every command
            # probes them, so a host that has them is one preflight
            # finds nothing wrong with.
            mock.patch.object(doctor.preflight, "check", return_value=None),
            mock.patch.object(hostruntime, "id",
                              return_value=hostruntime.LINUX),
            mock.patch.object(doctor, "_hostname", return_value="test-host"),
            mock.patch.object(update_check, "refresh"),
            mock.patch.object(update_check, "interactive", return_value=False),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(cli.main(["doctor"]), 0)
        output = stdout.getvalue()
        self.assertIn("Project checks skipped", output)
        self.assertIn("[PASS] crontab", output)
        self.assertIn("[PASS] inotifywait", output)
        self.assertIn("[PASS] copilot CLI", output)
        self.assertNotIn("Agents/ directory", output)
        self.assertNotIn("[PASS] project config", output)

    def test_doctor_prints_install_commands_for_missing_prerequisites(self) -> None:
        with (
            mock.patch.object(doctor, "REPO", None),
            mock.patch.object(doctor, "_has", return_value=False),
            mock.patch.object(doctor, "_python_312_resolvable", return_value=False),
            # The dispatch mechanisms are probed where every command
            # probes them, so an empty host is one that has neither.
            mock.patch.object(preflight.shutil, "which", return_value=None),
            mock.patch.object(hostruntime, "id",
                              return_value=hostruntime.LINUX),
            mock.patch.object(doctor, "_hostname", return_value="test-host"),
            mock.patch.object(update_check, "interactive", return_value=False),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(doctor.main([]), 1)

        output = stdout.getvalue()
        for command in (
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
            "uv python install 3.12",
            "npm i -g @anthropic-ai/claude-code",
            "npm i -g @github/copilot",
            "sudo apt install cron",
            "sudo apt install inotify-tools",
        ):
            self.assertIn(f"fix: {command}", output)

    def test_doctor_rejects_invalid_environment_root(self) -> None:
        os.environ[paths.ENV_VAR] = str(self.root / "missing")
        paths.clear_cache()
        self.assertEqual(cli.main(["doctor"]), 2)

    def test_dashboard_script_imports_in_packaged_layout(self) -> None:
        dashboard = Path(headless.__file__).with_name("dashboard.py")
        result = subprocess.run(
            ["uv", "run", "--script", str(dashboard), "--help"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--dev", result.stdout)

    def test_dashboard_refuses_a_port_another_server_answers_on(self) -> None:
        # Silent by construction: Windows lets a second listener bind an
        # address another process is serving, so before this check the
        # dashboard announced readiness and then sat unreachable behind
        # whatever already held the port (#174, #175).
        dashboard = Path(headless.__file__).with_name("dashboard.py")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            port = held.getsockname()[1]
            result = subprocess.run(
                ["uv", "run", "--script", str(dashboard), "--port", str(port)],
                capture_output=True,
                text=True,
                timeout=180,
            )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("port_unavailable", result.stderr)
        self.assertNotIn("ready to go", result.stdout)

    def _dashboards_main(self, *argv: str) -> tuple[int, str]:
        """Run the dashboard registry command and capture what it printed."""
        saved = sys.argv
        sys.argv = ["agents-live dashboard", *argv]
        try:
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                code = dashboards.main()
        finally:
            sys.argv = saved
        return code, stdout.getvalue()

    def test_dashboards_script_imports_in_packaged_layout(self) -> None:
        # Dispatched as a loose script, so its own imports have to resolve
        # without the package being installed (#198).
        script = Path(headless.__file__).with_name("dashboards.py")
        result = subprocess.run(
            ["uv", "run", "--script", str(script), "--help"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stop", result.stdout)

    def test_dashboard_list_reports_a_host_running_nothing(self) -> None:
        code, output = self._dashboards_main("list")
        self.assertEqual(code, 0)
        self.assertIn("No dashboard started by this host is running", output)

    def test_the_registry_drops_a_dashboard_whose_process_is_gone(self) -> None:
        # A killed dashboard never runs its exit hook, so the entry it
        # left behind would name a port nothing holds.
        gone = subprocess.Popen([sys.executable, "-c", ""])
        gone.wait()
        dashboards.record(8231, gone.pid, self.root)
        self.assertEqual(dashboards.running(), [])
        self.assertNotIn(
            str(gone.pid),
            dashboards.registry_path().read_text(encoding="utf-8"))

    def test_dashboard_stop_terminates_the_process_it_recorded(self) -> None:
        served = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(served.kill)
        dashboards.record(8231, served.pid, self.root)
        # The port probe is not what this test is about, and a real one
        # would depend on what else the host happens to be listening on.
        with mock.patch.object(dashboards, "port_answers", return_value=False):
            code, output = self._dashboards_main("stop", "--port", "8231")
        self.assertEqual(code, 0, output)
        self.assertIn("Stopped the dashboard on port 8231", output)
        # A terminated child stays a zombie on POSIX until its parent
        # reaps it, and a zombie still answers a liveness probe. Only
        # this test is the parent of the process it stopped; a real
        # dashboard is reaped by init.
        served.wait(timeout=30)
        self.assertFalse(hostruntime.is_alive(served.pid))
        self.assertEqual(dashboards.running(), [])

    def test_dashboard_stop_separates_a_relay_from_a_missing_entry(self) -> None:
        # Both cases are "not in the registry", and they need different
        # answers: something is holding the port that this host did not
        # start, or nothing is there at all.
        with mock.patch.object(dashboards, "port_answers", return_value=True):
            code, answering = self._dashboards_main("stop", "--port", "8231")
        self.assertEqual(code, 1)
        self.assertIn("not_found", answering)
        self.assertIn("this host did not start it", answering)
        with mock.patch.object(dashboards, "port_answers", return_value=False):
            _, silent = self._dashboards_main("stop", "--port", "8231")
        self.assertIn("nothing answers there", silent)

    def test_an_unreadable_registry_reads_as_an_empty_one(self) -> None:
        path = dashboards.registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ truncated", encoding="utf-8")
        self.assertEqual(dashboards.running(), [])

    def test_a_host_scoped_subcommand_needs_no_project_root(self) -> None:
        # `dashboard` resolves a root; `dashboard list` reports on this
        # host and must run from anywhere, so the gate reads the child's
        # declared kind rather than its parent's (#198).
        with (
            mock.patch.object(paths, "resolve_root",
                              side_effect=ValueError("no project root")),
            mock.patch.object(cli.subprocess, "run",
                              return_value=subprocess.CompletedProcess(
                                  [], 0)) as dispatched,
        ):
            self.assertEqual(cli.main(["dashboard", "list"]), 0)
            self.assertEqual(cli.main(["dashboard"]), 2)
        self.assertIn("dashboards.py", " ".join(dispatched.call_args[0][0]))

    def test_only_a_shared_script_is_told_which_action_ran(self) -> None:
        # `logs timeline` has a script to itself, so the token would be
        # an unrecognized argument; `dashboard list` and `dashboard stop`
        # share one, where the token is the only thing distinguishing
        # them.
        with mock.patch.object(
            cli.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as dispatched:
            cli.main(["dashboard", "stop", "--all"])
            shared = dispatched.call_args[0][0]
            cli.main(["logs", "timeline"])
            sole = dispatched.call_args[0][0]
        self.assertIn("stop", shared)
        self.assertNotIn("timeline", sole)

    def test_dashboard_structured_snapshot_deduplicates_correlated_errors(self) -> None:
        dashboard = Path(headless.__file__).with_name("dashboard.py")
        code = f'''
import importlib.util
import json
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from agents_live import headless

nicegui = types.ModuleType("nicegui")
nicegui.app = mock.MagicMock()
nicegui.ui = mock.MagicMock()
nicegui.run = mock.MagicMock()
sys.modules["nicegui"] = nicegui

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    logs = root / "logs"
    logs.mkdir()
    now = datetime.now(timezone.utc)
    base = {{
        "log_schema": 5, "agent_name": "alpha", "run_id": "run-1",
        "phase": "agent", "status": "error", "level": "error",
        "message": "failed",
    }}
    first = {{**base, "ts": now.isoformat(), "event_id": "event-1", "model": "old"}}
    duplicate = {{**base, "ts": (now + timedelta(milliseconds=1)).isoformat(),
                 "event_id": "event-2", "model": "new"}}
    framework = {{
        "ts": now.isoformat(), "log_schema": 5, "agent_name": "dashboard",
        "event_id": "event-3", "phase": "refresh", "status": "error",
        "level": "error", "message": "failed",
    }}
    (logs / "alpha.log").write_text(json.dumps(first) + "\\n", encoding="utf-8")
    (logs / "agents-live.log").write_text(json.dumps(duplicate) + "\\n", encoding="utf-8")
    (logs / "dashboard.log").write_text(json.dumps(framework) + "\\n", encoding="utf-8")

    headless.repo_root = mock.Mock(return_value=root)
    sys.argv = ["dashboard.py", "--all-repos"]
    spec = importlib.util.spec_from_file_location(
        "agents_live._dashboard_snapshot_test", {str(dashboard)!r})
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.LOGS_DIR = logs
    errors, models = module._structured_log_snapshot({{"alpha"}})
    assert errors == {{"alpha": 1, "framework": 1}}, errors
    assert models == {{"alpha": "new"}}, models
'''
        result = subprocess.run(
            ["uv", "run", "--with", "duckdb", "--with", "nicegui",
             "--with-editable", ".", "python", "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dashboard_rows_describe_an_agent_owned_by_another_runtime(self) -> None:
        """The Claim and Activate tips name the runtime to claim onto.

        Both tips are only reached when an agent is owned elsewhere, which
        is every agent immediately after an upgrade that changes the
        ownership format - exactly when the dashboard is needed to claim
        them back.
        """
        dashboard = Path(headless.__file__).with_name("dashboard.py")
        code = f'''
import importlib.util
import sys
import types
from unittest import mock

nicegui = types.ModuleType("nicegui")
nicegui.app = mock.MagicMock()
nicegui.ui = mock.MagicMock()
nicegui.run = mock.MagicMock()
sys.modules["nicegui"] = nicegui

sys.argv = ["dashboard.py", "--all-repos"]
spec = importlib.util.spec_from_file_location(
    "agents_live._dashboard_rows_test", {str(dashboard)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

module.collect_agents = lambda: [
    {{"name": "alpha", "state": "active", "runtime": "claude",
     "owner": "otherhost/ubuntu/uuid-b", "isOwner": False}},
    {{"name": "beta", "state": "active", "runtime": "claude",
     "owner": "thishost/ubuntu/uuid-a", "isOwner": True}},
]
module.last_runs = lambda name: ("-", "-", "")
module.agent_cost = lambda name: ("-", "-")
module.ownership.current_label = lambda: "thishost/ubuntu"
module.ownership.display_owner = lambda value: value
module.ownership.owns = lambda value: value.startswith("thishost/")

rows = {{row["name"]: row for row in module.agent_rows()}}

foreign = rows["alpha"]
assert foreign["can_claim"] is True, foreign
assert not foreign["local"], foreign
assert "thishost/ubuntu" in foreign["claim_tip"], foreign["claim_tip"]
assert "thishost/ubuntu" in foreign["activate_tip"], foreign["activate_tip"]

local = rows["beta"]
assert local["can_claim"] is False, local
assert local["claim_tip"] == "Already local", local["claim_tip"]
'''
        result = subprocess.run(
            ["uv", "run", "--with", "duckdb", "--with", "nicegui",
             "--with-editable", ".", "python", "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dashboard_without_a_project_says_so(self) -> None:
        """A page with no resolvable project must explain itself.

        A fully rendered dashboard whose agent table is empty is
        indistinguishable from broken agent discovery (issue #173), so
        the no-project page states what happened and how to select one.
        """
        dashboard = Path(headless.__file__).with_name("dashboard.py")
        code = f'''
import importlib.util
import sys
import types
from unittest import mock
from agents_live import paths, repos

nicegui = types.ModuleType("nicegui")
nicegui.app = mock.MagicMock()
nicegui.ui = mock.MagicMock()
nicegui.run = mock.MagicMock()
sys.modules["nicegui"] = nicegui
paths.resolve_root = mock.Mock(
    side_effect=ValueError("no project root found"))
repos.collect_status = mock.Mock(return_value={{"ok": True, "repos": []}})

sys.argv = ["dashboard.py", "--all-repos"]
spec = importlib.util.spec_from_file_location(
    "agents_live._dashboard_no_project_test", {str(dashboard)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

assert module.REPO_ROOT is None
assert module._scope_label() == "no project selected"
nicegui.ui.label.reset_mock()
module.build_page()
labels = [
    call.args[0] for call in nicegui.ui.label.call_args_list if call.args]
assert "No project selected" in labels, labels
assert any("repos default" in text for text in labels), labels
assert any("no project root found" in text for text in labels), labels
'''
        result = subprocess.run(
            ["uv", "run", "--with", "duckdb", "--with", "nicegui",
             "--with-editable", ".", "python", "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_package_has_no_undefined_names(self) -> None:
        """No module-level reference to a name that only exists elsewhere.

        A name defined inside one function and read from another raises
        NameError only when that branch runs, so a rarely taken path ships
        broken. Only undefined names are treated as failures; style
        findings would make the check noisy enough to be ignored.
        """
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["uv", "run", "--with", "pyflakes", "python", "-m", "pyflakes",
             str(root / "src" / "agents_live"), str(root / "tools")],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0 and not result.stdout:
            self.skipTest(f"pyflakes unavailable: {result.stderr.strip()}")
        undefined = [
            line for line in result.stdout.splitlines()
            if "undefined name" in line
        ]
        self.assertEqual(undefined, [], "\n".join(undefined))

    def test_rootless_all_repos_dashboard_has_no_relative_paths(self) -> None:
        dashboard = Path(headless.__file__).with_name("dashboard.py")
        code = f"""
import asyncio
import importlib.util
import sys
import types
from unittest import mock
from agents_live import paths, repos

nicegui = types.ModuleType("nicegui")
nicegui.app = mock.MagicMock()
nicegui.ui = mock.MagicMock()
nicegui.run = mock.MagicMock()
sys.modules["nicegui"] = nicegui
paths.resolve_root = mock.Mock(side_effect=ValueError("no root"))
repos.collect_status = mock.Mock(return_value={{"ok": True, "repos": []}})
sys.argv = ["dashboard.py", "--all-repos"]
spec = importlib.util.spec_from_file_location(
    "agents_live._rootless_dashboard_test", {str(dashboard)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.REPO_ROOT is None
assert module.LOGS_DIR is None
assert module.DASHBOARD_LOG is None
assert module.DASHBOARD_TRANSCRIPT is None
# The health beacon is host-scoped now: absolute, under the state home,
# and available with no repository selected.
from agents_live import paths
assert module.HEALTH_OK_PATH == paths.health_beacon_path()
assert module.HEALTH_OK_PATH.is_absolute()

async def exercise_queue():
    module.output_log = mock.MagicMock()
    calls = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def execute(request):
        calls.append(request.description)
        if request.label == "First":
            first_started.set()
            await release_first.wait()
            return 7
        return 0

    module._execute_action = execute
    first = asyncio.create_task(module.do_action("First", "run.py", ["--name", "one"]))
    await first_started.wait()
    second = asyncio.create_task(module.do_action("Second", "run.py", ["--name", "two"]))
    await asyncio.sleep(0)
    duplicate = asyncio.create_task(module.do_action("Second", "run.py", ["--name", "two"]))
    await asyncio.sleep(0)
    assert len(module._ACTION_QUEUE) == 1
    release_first.set()
    assert await asyncio.gather(first, second, duplicate) == [7, 0, 0]
    assert calls == ["First --name one", "Second --name two"]

    finalized = []
    refreshed = []

    async def execute_with_exception(request):
        calls.append(request.description)
        if request.label == "Broken":
            raise OSError("cannot spawn")
        return 0

    module._execute_action = execute_with_exception
    module._log_action = lambda *args, **kwargs: finalized.append((args, kwargs))
    module._refresh_views = lambda: refreshed.append(True)
    broken = asyncio.create_task(module.do_action("Broken", "run.py", ["--name", "bad"]))
    after = asyncio.create_task(module.do_action("After", "run.py", ["--name", "good"]))
    assert await asyncio.gather(broken, after) == [-1, 0]
    assert finalized[0][0][3] == -1
    assert "cannot spawn" in finalized[0][0][4]
    assert refreshed == [True]
    assert calls[-2:] == ["Broken --name bad", "After --name good"]

asyncio.run(exercise_queue())

rows = [
    {{"name": "alpha", "state": "active", "owner": "host-a", "agent": "copilot",
      "unhealthy": False, "cost_day": "$1.25", "cost_week": "$3.50"}},
    {{"name": "beta", "state": "stopped", "owner": "host-b", "agent": "handler",
      "unhealthy": True, "cost_day": "-", "cost_week": "-"}},
]
assert module._filtered_agent_rows(rows, {{"name": "bet", "state": "All",
    "owner": "All", "runtime": "All", "failing": True}}) == [rows[1]]
assert module._cost_totals(rows) == ("$1.25", "$3.50")
assert module._agent_model({{"name": "llm", "runtime": "copilot", "model": "configured"}},
    {{"llm": "reported"}}) == "reported"
assert module._agent_model({{"name": "llm", "runtime": "copilot", "model": "configured"}},
    {{}}) == "configured"
assert module._agent_model({{"name": "llm", "runtime": "copilot"}}, {{}}) == "default"
assert module._agent_model({{"name": "handler", "runtime": "none"}},
    {{"handler": "reported"}}) == "-"
"""
        with tempfile.TemporaryDirectory() as outside:
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=outside,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(outside) / "Agents").exists())
        source = dashboard.read_text(encoding="utf-8")
        self.assertIn('ui.label("Log")', source)
        self.assertIn('"label": "$/24h"', source)
        self.assertIn('"label": "$/1w"', source)
        self.assertIn('"label": "Model"', source)
        self.assertIn("agent-table-scroll", source)
        self.assertIn("minmax(15rem,.7fr)", source)

    def test_doctor_reads_update_status_without_refreshing_cache(self) -> None:
        cache = self.root / "cache" / "agents-live" / "update-check.json"
        cache.parent.mkdir(parents=True)
        cache.write_text('{"checked_at": 100, "latest_version": null}\n',
                         encoding="utf-8")
        before = cache.read_bytes()
        with (
            mock.patch.object(update_check, "cache_path", return_value=cache),
            mock.patch.object(doctor, "collect", return_value=[]),
            mock.patch.object(doctor, "_hostname", return_value="test-host"),
            mock.patch.object(update_check, "refresh") as refresh,
            mock.patch.object(
                update_check, "status_text", return_value="Update check: current") as status,
            mock.patch.object(update_check, "interactive", return_value=True),
            mock.patch("sys.stdout", io.StringIO()),
            mock.patch("sys.stderr", io.StringIO()),
        ):
            self.assertEqual(doctor.main([]), 0)
        refresh.assert_not_called()
        status.assert_called_once()
        self.assertEqual(cache.read_bytes(), before)

    def test_doctor_children_suppress_redundant_update_refresh(self) -> None:
        completed = subprocess.CompletedProcess(
            [], 0, stdout='{"ok": true}', stderr="")
        with (
            mock.patch.object(repos, "_cli_base", return_value=["agents-live"]),
            mock.patch.object(
                repos.subprocess, "run", return_value=completed) as run,
        ):
            result = repos._child_json("project", "/project", "doctor")
        self.assertTrue(result["ok"])
        self.assertEqual(
            run.call_args.kwargs["env"][repos.SKIP_UPDATE_CHECK_ENV], "1")


    def test_doctor_json_suppresses_cached_update_result(self) -> None:
        with (
            mock.patch.object(doctor, "collect", return_value=[]),
            mock.patch.object(doctor, "_hostname", return_value="test-host"),
            mock.patch.object(update_check, "refresh") as refresh,
            mock.patch.object(update_check, "status_text") as status,
            mock.patch.object(update_check, "interactive", return_value=True),
            mock.patch.dict(os.environ, {preflight.JSON_ENV_VAR: "1"}),
            mock.patch("sys.stdout", io.StringIO()),
            mock.patch("sys.stderr", io.StringIO()),
        ):
            self.assertEqual(doctor.main([]), 0)
        refresh.assert_not_called()
        status.assert_not_called()

    def test_doctor_json_flag_positions_are_equivalent(self) -> None:
        def invoke(argv: list[str]) -> dict:
            stdout = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"AGENTS_LIVE_JSON": ""}),
                mock.patch.object(doctor, "collect", return_value=[]),
                mock.patch.object(doctor, "_hostname", return_value="test-host"),
                mock.patch.object(update_check, "refresh"),
                mock.patch("sys.stdout", stdout),
            ):
                self.assertEqual(cli.main(argv), 0)
            return json.loads(stdout.getvalue())

        self.assertEqual(
            invoke(["--json", "doctor"]),
            invoke(["doctor", "--json"]),
        )


class TestPreReleaseAudit(unittest.TestCase):
    @staticmethod
    def _module():
        audit_path = (
            Path(__file__).resolve().parents[1] / "tools" /
            "pre-release-audit.py")
        spec = importlib.util.spec_from_file_location(
            "agents_live_pre_release_audit", audit_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_machine_name_file_absent_comments_and_matches(self) -> None:
        audit = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(audit.load_machine_names(root), [])
            (root / audit.MACHINE_NAMES_FILE).write_text(
                "\n# local names\nprivate-host-fixture\n", encoding="utf-8")
            names = audit.load_machine_names(root)
            self.assertEqual(names, ["private-host-fixture"])
            shipped = root / "README.md"
            shipped.write_text(
                "Deployed on PRIVATE-HOST-FIXTURE.\n", encoding="utf-8")
            findings = audit.scan_file(shipped, root, names)
            self.assertEqual(len(findings), 1)
            self.assertIn(audit.MACHINE_NAMES_FILE, findings[0])

    def test_em_dash_in_markdown_is_rejected(self) -> None:
        audit = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shipped = root / "README.md"
            shipped.write_text("left — right\n", encoding="utf-8")
            self.assertIn(
                "Em dash in shipped Markdown",
                audit.scan_file(shipped, root)[0],
            )

    def test_a_wsl_home_reached_from_windows_is_rejected(self) -> None:
        # The POSIX home pattern is forward-slashed, so it never saw a
        # WSL home reached over UNC from the Windows side (#213).
        audit = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shipped = root / "README.md"
            shipped.write_text(
                r"Run from \\wsl.localhost\Ubuntu\home\jane\project." "\n",
                encoding="utf-8")
            self.assertIn(
                "WSL home directory", audit.scan_file(shipped, root)[0])

    def test_the_wsl_prefix_alone_stays_publishable(self) -> None:
        # docs/windows-support.md names the namespace repeatedly while
        # explaining why the tool refuses it. The pattern must not fire
        # on a prefix carrying no user name.
        audit = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shipped = root / "README.md"
            shipped.write_text(
                r"It refuses \\wsl.localhost and \\wsl$ repositories." "\n",
                encoding="utf-8")
            self.assertEqual(audit.scan_file(shipped, root), [])

    def test_a_personal_path_in_a_name_is_rejected(self) -> None:
        # Contents are scanned by extension; a name ships whatever the
        # file holds, and two files named for absolute temp paths once
        # survived several releases.
        audit = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stray = root / "docs" / "Users" / "jane"
            stray.mkdir(parents=True)
            (stray / "scratch.bin").write_bytes(b"\x00")
            findings = audit.scan_names(root)
            self.assertEqual(len(findings), 1)
            self.assertIn("macOS user in path", findings[0])

    def test_a_machine_name_in_a_path_is_rejected(self) -> None:
        audit = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "PRIVATE-HOST-FIXTURE-setup.md").write_text(
                "notes\n", encoding="utf-8")
            findings = audit.scan_names(root, ["private-host-fixture"])
            self.assertEqual(len(findings), 1)
            self.assertIn("Known machine name in path", findings[0])

    def test_exclusions_hold_on_a_windows_checkout(self) -> None:
        # EXCLUDED_PATTERNS is forward-slashed and was compared against
        # str(relative), which is backslashed on Windows, so the runtime
        # log and data directories were excluded on POSIX only.
        audit = self._module()
        for excluded in ("Agents/logs/an-agent.log",
                         "Agents/data/state.json",
                         "src/agents_live/__pycache__/run.pyc"):
            self.assertTrue(audit.is_excluded(Path(excluded)), excluded)
        self.assertFalse(audit.is_excluded(Path("src/agents_live/run.py")))


class TestHostRuntimeIdentity(unittest.TestCase):
    """The seam member every host-specific branch now reads."""

    def _proc_version(self, text: str) -> Path:
        version = Path(self._tmp.name) / "proc-version"
        version.write_text(text, encoding="utf-8")
        return version

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_wsl_kernel_string_identifies_wsl(self) -> None:
        version = self._proc_version(
            "Linux version 5.15.0-microsoft-standard-WSL2 (gcc ...)\n")
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(hostruntime, "PROC_VERSION", version),
        ):
            self.assertEqual(hostruntime.id(), hostruntime.WSL)

    def test_plain_kernel_string_identifies_linux(self) -> None:
        version = self._proc_version("Linux version 6.8.0-generic (gcc ...)\n")
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(hostruntime, "PROC_VERSION", version),
        ):
            self.assertEqual(hostruntime.id(), hostruntime.LINUX)

    def test_missing_proc_version_identifies_linux(self) -> None:
        absent = Path(self._tmp.name) / "does-not-exist"
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(hostruntime, "PROC_VERSION", absent),
        ):
            self.assertEqual(hostruntime.id(), hostruntime.LINUX)

    def test_windows_is_identified_without_reading_proc(self) -> None:
        absent = Path(self._tmp.name) / "does-not-exist"
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(hostruntime, "PROC_VERSION", absent),
        ):
            self.assertEqual(hostruntime.id(), hostruntime.WINDOWS)


class TestHostRuntimeLock(unittest.TestCase):
    """The lock contract both platforms have to honour identically."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock_path = Path(self._tmp.name) / "nested" / "probe.lock"

    def test_holder_excludes_a_second_acquisition(self) -> None:
        with hostruntime.exclusive_lock(self.lock_path):
            with self.assertRaises(hostruntime.LockBusy):
                with hostruntime.exclusive_lock(self.lock_path):
                    pass

    def test_lock_is_free_again_after_the_block_exits(self) -> None:
        with hostruntime.exclusive_lock(self.lock_path):
            pass
        with hostruntime.exclusive_lock(self.lock_path):
            pass

    def test_lock_is_released_when_the_block_raises(self) -> None:
        with self.assertRaises(ZeroDivisionError):
            with hostruntime.exclusive_lock(self.lock_path):
                raise ZeroDivisionError
        with hostruntime.exclusive_lock(self.lock_path):
            pass

    def test_blocking_acquisition_waits_for_the_holder(self) -> None:
        def hold() -> None:
            with hostruntime.exclusive_lock(self.lock_path):
                time.sleep(0.3)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join)
        time.sleep(0.05)
        started = time.monotonic()
        with hostruntime.exclusive_lock(self.lock_path, blocking=True):
            waited = time.monotonic() - started
        self.assertGreater(waited, 0.1)

    def test_owner_metadata_in_the_lock_file_stays_readable(self) -> None:
        """Windows locks are mandatory, so the locked byte lives past any content."""
        with hostruntime.exclusive_lock(self.lock_path):
            with self.lock_path.open("r+", encoding="utf-8") as handle:
                handle.write("owner metadata\n")
                handle.truncate()
            self.assertEqual(self.lock_path.read_text(encoding="utf-8"),
                             "owner metadata\n")


class TestHostRuntimeProcesses(unittest.TestCase):
    """Detached spawning, liveness, and termination of a whole tree."""

    def _await_exit(self, pid: int, timeout_s: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and hostruntime.is_alive(pid):
            time.sleep(0.05)

    def test_spawned_child_is_alive_then_terminates(self) -> None:
        child = hostruntime.spawn_detached(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertTrue(hostruntime.is_alive(child.pid))
        hostruntime.terminate(child.pid, grace_s=5)
        child.wait(timeout=15)
        self.assertFalse(hostruntime.is_alive(child.pid))

    def test_terminate_reaches_a_grandchild(self) -> None:
        parent_source = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen("
            "[sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        parent = hostruntime.spawn_detached(
            [sys.executable, "-c", parent_source],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(parent.wait)
        self.addCleanup(parent.kill)
        self.addCleanup(parent.stdout.close)
        grandchild_pid = int(parent.stdout.readline().strip())
        self.assertTrue(hostruntime.is_alive(grandchild_pid))

        hostruntime.terminate(parent.pid, grace_s=10)
        parent.wait(timeout=15)
        self._await_exit(grandchild_pid)
        self.assertFalse(hostruntime.is_alive(grandchild_pid))

    def test_is_alive_is_false_for_an_unused_pid(self) -> None:
        self.assertFalse(hostruntime.is_alive(0x7FFFFFFE))

    def test_terminating_a_dead_pid_is_quiet(self) -> None:
        child = hostruntime.spawn_detached([sys.executable, "-c", "pass"])
        child.wait(timeout=15)
        hostruntime.terminate(child.pid, grace_s=1)

    def test_a_detached_child_owns_a_console_nobody_can_see(self) -> None:
        """The property that keeps consoles from flashing on the desktop.

        Measured: with ``DETACHED_PROCESS`` set, Windows ignores
        ``CREATE_NO_WINDOW`` and gives the child a fresh console whose
        window handle is real - the flash. Without it the child still
        gets a console, so descendants inherit one, but the window
        handle is zero and nothing is ever drawn.
        """
        if hostruntime.id() != hostruntime.WINDOWS:
            self.skipTest("only Windows attaches consoles to processes")
        source = (
            "import ctypes\n"
            "kernel32 = ctypes.windll.kernel32\n"
            "print(kernel32.GetConsoleCP(), kernel32.GetConsoleWindow(),"
            " flush=True)\n"
        )
        child = hostruntime.spawn_detached(
            [sys.executable, "-c", source], stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.addCleanup(child.stdout.close)
        code_page, console_window = child.stdout.readline().split()
        self.assertNotEqual(code_page, "0")
        self.assertEqual(console_window, "0")


class TestReadingTheProcessTable(unittest.TestCase):
    """Where the command lines come from, and what happens when they don't."""

    def _windows_only(self) -> None:
        if hostruntime.id() != hostruntime.WINDOWS:
            self.skipTest("the direct read is a Windows-only path")

    def test_the_reader_names_this_process_and_its_arguments(self) -> None:
        # The contract every caller depends on: the arguments, not just
        # the executable, because the arguments say which agent a
        # watcher belongs to.
        table = dict(hostruntime.process_command_lines())
        self.assertIn(os.getpid(), table)
        self.assertIn(Path(sys.executable).stem,
                      table[os.getpid()].replace("\\", "/"))

    def test_the_direct_read_names_this_process(self) -> None:
        self._windows_only()
        table = dict(hostruntime._command_lines_in_process())
        self.assertGreater(len(table), 1)  # a host runs more than us
        self.assertIn(os.getpid(), table)
        self.assertIn(Path(sys.executable).stem,
                      table[os.getpid()].replace("\\", "/"))

    def test_a_dead_pid_reads_as_nothing_rather_than_raising(self) -> None:
        self._windows_only()
        self.assertIsNone(hostruntime._command_line(0x7FFFFFFE))

    def test_cim_takes_over_when_the_direct_read_comes_back_empty(self) -> None:
        # Empty is the signal, because this process is always readable
        # by itself: a direct read that finds nothing did not work at
        # all. ProcessCommandLineInformation is Windows 8.1 and later
        # and ntdll is not a contract, so the supported read has to
        # stand behind it.
        self._windows_only()
        with (
            mock.patch.object(hostruntime, "_command_lines_in_process",
                              return_value=[]),
            mock.patch.object(hostruntime, "_command_lines_via_cim",
                              return_value=[(7, "agents-live watch-loop x")]),
        ):
            self.assertEqual(hostruntime.process_command_lines(),
                             [(7, "agents-live watch-loop x")])

    def test_an_unbound_ntdll_reads_as_empty(self) -> None:
        self._windows_only()
        with mock.patch.object(hostruntime, "_nt_query_process", None):
            self.assertEqual(hostruntime._command_lines_in_process(), [])

    def test_cim_is_not_paid_for_when_the_direct_read_works(self) -> None:
        self._windows_only()
        with (
            mock.patch.object(hostruntime, "_command_lines_in_process",
                              return_value=[(7, "agents-live watch-loop x")]),
            mock.patch.object(hostruntime, "_command_lines_via_cim") as cim,
        ):
            self.assertEqual(hostruntime.process_command_lines(),
                             [(7, "agents-live watch-loop x")])
        cim.assert_not_called()


class TestEnumerationPasses(_TempProject):
    """Asking the host once for what answers about the whole host.

    A process table or a folder of registered tasks describes the
    machine, not an agent, but the callers that want them are per-agent
    loops. On Windows each of those reads costs a subprocess and about
    two seconds, so a dashboard that asked per agent could not finish a
    page. What is asserted here is the count, because the count is the
    bug.
    """

    def test_a_host_read_outside_a_pass_is_never_remembered(self) -> None:
        # Inside a declared pass the read happens once. Outside one it
        # happens every time, because an action changes the host and the
        # read after it has to see the change: a cache with a lifetime
        # would answer that read from before the action.
        reads = []
        with hostruntime.enumeration_pass():
            for _ in range(5):
                hostruntime.pass_cached("probe", lambda: reads.append(1))
        self.assertEqual(len(reads), 1)
        for _ in range(5):
            hostruntime.pass_cached("probe", lambda: reads.append(1))
        self.assertEqual(len(reads), 6)

    def test_an_inner_pass_joins_the_outer_one(self) -> None:
        # A caller declares a pass without knowing whether one of its
        # callers already did, so nesting must not restart the reads.
        reads = []
        with hostruntime.enumeration_pass():
            hostruntime.pass_cached("probe", lambda: reads.append(1))
            with hostruntime.enumeration_pass():
                hostruntime.pass_cached("probe", lambda: reads.append(1))
        self.assertEqual(len(reads), 1)
        with hostruntime.enumeration_pass():
            hostruntime.pass_cached("probe", lambda: reads.append(1))
        self.assertEqual(len(reads), 2)

    def _watcher_command(self, name: str) -> str:
        argv = ["agents-live", "--repo", str(self.root),
                "internal", "watch-loop", name]
        if sys.platform == "win32":
            return subprocess.list2cmdline(argv)
        return " ".join(argv)

    def test_a_sweep_reads_the_process_table_once(self) -> None:
        table = [(4321, self._watcher_command("alpha")),
                 (8765, self._watcher_command("beta"))]
        with mock.patch.object(hostruntime, "process_command_lines",
                               return_value=table) as read:
            with hostruntime.enumeration_pass():
                self.assertEqual(headless._find_watcher_pids_table("alpha"),
                                 [4321])
                self.assertEqual(headless._find_watcher_pids_table("beta"),
                                 [8765])
                self.assertEqual(headless._find_watcher_pids_table("gamma"), [])
        read.assert_called_once()

    def test_a_multi_trigger_agent_is_asked_once_per_trigger(self) -> None:
        # The state word and the per-trigger detail are the same two
        # questions. Asking them twice doubled every status sweep.
        from agents_live import schedules

        self.write_agent("multi", MULTI_TRIGGER_DEFINITION)
        config = headless.load_agent_config("multi")
        self.assertEqual(config.trigger_type, "multi")
        with (
            mock.patch.object(schedules, "is_active",
                              return_value=True) as scheduled,
            mock.patch.object(headless, "find_watcher_pid",
                              return_value=4321) as watching,
        ):
            details = headless.agent_details(config)
        scheduled.assert_called_once_with("multi")
        watching.assert_called_once_with("multi")
        self.assertEqual(details["state"], "active")
        self.assertEqual(details["triggerStates"],
                         {"cron": "active", "watcher": "active (pid 4321)"})

    def test_a_partly_stopped_agent_still_reads_as_partial(self) -> None:
        # Taking the reading instead of making one must not change what
        # the word means.
        from agents_live import schedules

        self.write_agent("multi", MULTI_TRIGGER_DEFINITION)
        config = headless.load_agent_config("multi")
        with (
            mock.patch.object(schedules, "is_active", return_value=True),
            mock.patch.object(headless, "find_watcher_pid", return_value=None),
        ):
            self.assertEqual(headless.agent_details(config)["state"], "partial")


class TestStaleIdentityAndPathAliases(_TempProject):
    """Two ways a lifecycle operation ends up aimed at the wrong thing.

    A pid outlives nothing on a desktop that stays up for weeks: the
    number is handed back out, and a stop that trusts a remembered pid
    signals whatever now holds it. A path is no safer - a junction
    reaches one repository under a second name, and a second name is a
    second set of triggers unless it resolves to the first. Neither is
    exotic on a developer's own machine, which is the only kind this
    tool runs on.
    """

    def setUp(self) -> None:
        super().setUp()
        self._other = tempfile.TemporaryDirectory()
        self.addCleanup(self._other.cleanup)
        self.outside = Path(self._other.name).resolve()

    def watcher_command(self, name: str, root: Path | None = None) -> str:
        """A packaged watch loop's command line, joined as its host joins."""
        argv = ["agents-live", "--repo", str(root or self.root),
                "internal", "watch-loop", name]
        if sys.platform == "win32":
            return subprocess.list2cmdline(argv)
        return " ".join(argv)

    def test_a_live_pid_running_something_else_is_not_our_watcher(self) -> None:
        # The pid is real and alive, which is all a remembered pid can
        # ever prove. What the process runs is what decides.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        table = [(child.pid, "some-other-tool --repo "
                             f"{self.root} internal watch-loop todo")]
        with mock.patch.object(hostruntime, "process_command_lines",
                               return_value=table):
            self.assertEqual(headless._find_watcher_pids_table("todo"), [])
        self.assertTrue(hostruntime.is_alive(child.pid))

    def test_a_watcher_is_found_by_what_it_runs_not_by_a_pid(self) -> None:
        # Nothing is remembered between runs, so any pid the host is
        # using answers - including one that used to be a watcher's.
        command = self.watcher_command("todo")
        for pid in (4321, 9876):
            with mock.patch.object(hostruntime, "process_command_lines",
                                   return_value=[(pid, command)]):
                self.assertEqual(
                    headless._find_watcher_pids_table("todo"), [pid])

    def test_a_same_named_watcher_in_another_project_is_never_ours(self) -> None:
        with mock.patch.object(
                hostruntime, "process_command_lines",
                return_value=[(4321,
                               self.watcher_command("todo", self.outside))]):
            self.assertEqual(headless._find_watcher_pids_table("todo"), [])

    def test_a_repository_path_with_a_space_survives_the_round_trip(self) -> None:
        if sys.platform != "win32":
            self.skipTest("only Windows reads command lines back with quoting")
        spaced = self.root / "a project"
        (spaced / "Agents" / "data").mkdir(parents=True)
        (spaced / ".agents-live.toml").write_text("", encoding="utf-8")
        os.environ[paths.ENV_VAR] = str(spaced)
        paths.clear_cache()
        with mock.patch.object(
                hostruntime, "process_command_lines",
                return_value=[(4321, self.watcher_command("todo", spaced))]):
            self.assertEqual(headless._find_watcher_pids_table("todo"), [4321])

    def test_a_project_opened_through_a_junction_is_one_project(self) -> None:
        # Two spellings, one root: otherwise the same project registers
        # two sets of triggers and each is invisible to the other.
        alias = self.outside / "alias"
        link_directory(alias, self.root)
        self.assertEqual(paths.resolve_root(alias), self.root)

    def test_a_junction_inside_the_repository_is_an_agent_directory(self) -> None:
        real = self.root / "real-agents"
        real.mkdir()
        link_directory(self.root / "linked", real)
        self.assertEqual(
            paths.validated_agent_directories(self.root, ["linked"]), [real])

    def test_a_junction_retargeted_outside_stops_being_accepted(self) -> None:
        # The reparse point is re-read every time, so a directory that
        # was inside the repository yesterday is refused today.
        real = self.root / "real-agents"
        real.mkdir()
        link = self.root / "linked"
        link_directory(link, real)
        paths.validated_agent_directories(self.root, ["linked"])

        unlink_directory(link)
        link_directory(link, self.outside)
        with self.assertRaisesRegex(ValueError, "escapes"):
            paths.validated_agent_directories(self.root, ["linked"])


class TestHostRuntimeEnvironment(unittest.TestCase):
    """The environment, PATH, and executable an agent run is launched with."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.windows = hostruntime.id() == hostruntime.WINDOWS

    def test_base_env_carries_a_home_for_the_agent_cli(self) -> None:
        self.assertTrue(hostruntime.base_env().get("HOME"))

    def test_base_env_carries_what_the_host_cannot_start_without(self) -> None:
        env = hostruntime.base_env()
        if self.windows:
            # Measured: a native CLI launched without SystemRoot dies in
            # the loader with STATUS_STACK_BUFFER_OVERRUN (0xC0000409)
            # and writes nothing to either stream.
            self.assertIn("SystemRoot", env)
        else:
            self.assertEqual(set(env), {"HOME"})

    def test_system_path_dirs_are_absolute(self) -> None:
        dirs = hostruntime.system_path_dirs()
        self.assertTrue(dirs)
        for entry in dirs:
            self.assertTrue(Path(entry).is_absolute(), entry)

    def test_constructed_path_carries_the_system_directories(self) -> None:
        entries = headless.clean_path().split(os.pathsep)
        for entry in hostruntime.system_path_dirs():
            self.assertIn(entry, entries)

    def test_constructed_path_inherits_only_where_the_host_does(self) -> None:
        marker = str(Path(self._tmp.name) / "marker-bin")
        with mock.patch.dict(os.environ, {"PATH": marker}):
            entries = headless.clean_path().split(os.pathsep)
        self.assertEqual(marker in entries, hostruntime.inherits_path())

    def test_agent_env_is_built_rather_than_inherited(self) -> None:
        config = headless.AgentConfig(
            name="probe", prompt_path=Path(self._tmp.name) / "probe.md",
            resolved=True)
        with mock.patch.dict(os.environ, {"AGENTS_LIVE_LEAK_PROBE": "1"}):
            env = headless._build_agent_env(config)
        self.assertNotIn("AGENTS_LIVE_LEAK_PROBE", env)
        self.assertEqual(env["PATH"], headless.clean_path())

    def test_find_tool_reports_nothing_for_an_unknown_name(self) -> None:
        self.assertIsNone(hostruntime.find_tool("agents-live-no-such-tool"))

    def test_pin_executable_resolves_what_this_host_has_to_resolve(self) -> None:
        directory = str(Path(sys.executable).parent)
        name = Path(sys.executable).stem
        pinned = hostruntime.pin_executable(name, path=directory)
        if self.windows:
            self.assertTrue(Path(pinned).is_absolute())
            self.assertTrue(Path(pinned).is_file())
        else:
            # execvp searches the child's own PATH, so the name is the pin.
            self.assertEqual(pinned, name)

    def test_pin_executable_refuses_a_shim_it_cannot_launch(self) -> None:
        if not self.windows:
            self.skipTest("only Windows resolves a name to a shim")
        directory = Path(self._tmp.name)
        (directory / "probe.bat").write_text("@echo off\n", encoding="utf-8")
        with self.assertRaises(hostruntime.ExecutableNotFound):
            hostruntime.pin_executable("probe", path=str(directory))

    def test_pin_executable_refuses_a_name_nothing_answers_to(self) -> None:
        if not self.windows:
            self.assertEqual(
                hostruntime.pin_executable("agents-live-no-such-tool"),
                "agents-live-no-such-tool")
            return
        with self.assertRaises(hostruntime.ExecutableNotFound):
            hostruntime.pin_executable("agents-live-no-such-tool",
                                       path=self._tmp.name)

    def test_shell_handler_runs_only_where_there_is_a_shell(self) -> None:
        handler = Path(self._tmp.name) / "handler.sh"
        handler.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shell = hostruntime.shell_interpreter()
        if shell is None:
            with self.assertRaises(headless.AgentsLiveError):
                headless._build_handler_command(handler)
        else:
            self.assertEqual(headless._build_handler_command(handler),
                             [*shell, str(handler)])

    def test_node_handler_needs_no_shell_on_any_host(self) -> None:
        handler = Path(self._tmp.name) / "handler.js"
        handler.write_text("process.exit(0)\n", encoding="utf-8")
        self.assertEqual(headless._build_handler_command(handler),
                         ["node", str(handler)])

    def test_utf8_io_reaches_the_interpreters_this_process_launches(self) -> None:
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("PYTHONUTF8", None)
            hostruntime.use_utf8_io()
            self.assertEqual(os.environ["PYTHONUTF8"], "1")


class TestWindowsScheduling(unittest.TestCase):
    """What a Windows host would be told to schedule.

    Everything here is the pure half of the task store: what the command
    string says, what the task is called, and what the XML asks for. It
    runs on every platform because that is what decides whether the
    Windows half does the right thing, and on Windows the round trip is
    checked by the same function that will parse the string for real.
    """

    ROOT = Path("C:\\Users\\dev\\projects\\demo")
    AWKWARD = [
        "plain",
        "with space",
        r"C:\Program Files\repo",
        "C:\\ends\\with\\sep\\",
        'quote"inside',
        r"back\\slashes",
        "trailing\\",
        "",
    ]

    def test_an_argument_string_parses_back_to_its_arguments(self) -> None:
        line = wintasks.argument_string(self.AWKWARD)
        self.assertEqual(
            wintasks.parse_command_line(f'"prog" {line}')[1:], self.AWKWARD)

    @contextlib.contextmanager
    def _recording_task_store(self, registered: dict[str, object]):
        """A store that reads back exactly what was written to it.

        Registration verifies its own read-back, so a stub document
        would fail the write it is meant to stand in for. Keeping the
        real document also means these tests exercise the round trip
        rather than asserting on the call that started it.
        """
        written: dict[str, str] = {}
        build_task_xml = wintasks.build_task_xml

        def build(**kwargs):
            registered.update(kwargs)
            written["document"] = build_task_xml(**kwargs)
            return written["document"]

        with (
            mock.patch.object(wintasks, "read_definition",
                              side_effect=lambda _path: written.get("document")),
            mock.patch.object(wintasks, "current_user_id",
                              return_value="EXAMPLE\\dev"),
            mock.patch.object(wintasks, "_run", return_value=(0, "", "")),
            mock.patch.object(wintasks, "build_task_xml", side_effect=build),
        ):
            yield

    def test_an_agent_with_no_registered_task_costs_no_definition_reads(
            self) -> None:
        # The folder listing is one query for the whole host. Where it
        # names nothing of this agent's, there is nothing to read.
        with (
            mock.patch.object(wintasks, "registered_task_names",
                              return_value=[]),
            mock.patch.object(wintasks, "read_definition") as read,
        ):
            self.assertFalse(wintasks.is_active(self.ROOT, "todo"))
        read.assert_not_called()

    def test_only_the_kinds_the_folder_lists_are_read(self) -> None:
        watcher = wintasks.task_name(self.ROOT, "todo", kind=wintasks.WATCH)
        with (
            # Upper case because the store compares names that way and
            # returns whatever spelling it holds.
            mock.patch.object(wintasks, "registered_task_names",
                              return_value=[watcher.upper()]),
            mock.patch.object(wintasks, "read_definition",
                              return_value="<Task/>") as read,
            mock.patch.object(wintasks, "_is_ours", return_value=True),
        ):
            self.assertTrue(wintasks.is_active(self.ROOT, "todo"))
        read.assert_called_once_with(
            wintasks.task_path(self.ROOT, "todo", kind=wintasks.WATCH))

    def test_the_registered_xml_still_decides_ownership(self) -> None:
        # A listed name is only a reason to read the definition. What
        # the definition says is what makes the task ours.
        watcher = wintasks.task_name(self.ROOT, "todo", kind=wintasks.WATCH)
        with (
            mock.patch.object(wintasks, "registered_task_names",
                              return_value=[watcher]),
            mock.patch.object(wintasks, "read_definition",
                              return_value="<Task/>"),
            mock.patch.object(wintasks, "_is_ours", return_value=False),
        ):
            self.assertFalse(wintasks.is_active(self.ROOT, "todo"))

    def test_a_store_that_will_not_list_is_asked_the_long_way(self) -> None:
        with (
            mock.patch.object(wintasks, "registered_task_names",
                              return_value=None),
            mock.patch.object(wintasks, "read_definition",
                              return_value=None) as read,
        ):
            self.assertFalse(wintasks.is_active(self.ROOT, "todo"))
        self.assertEqual(read.call_count, 3)

    def test_the_task_folder_is_listed_once_per_enumeration_pass(self) -> None:
        with mock.patch.object(wintasks, "_read_task_names",
                               return_value=[]) as listing:
            with hostruntime.enumeration_pass():
                for agent in ("todo", "notes", "digest"):
                    self.assertFalse(wintasks.is_active(self.ROOT, agent))
                self.assertEqual(wintasks.installed_names(self.ROOT), [])
        listing.assert_called_once()

    def test_a_directory_argument_keeps_its_trailing_separator(self) -> None:
        # The backslash before the closing quote is the case every
        # Windows path argument runs into.
        args = ["--repo", "C:\\Users\\dev\\my repo\\"]
        parsed = wintasks.parse_command_line(
            f'"prog" {wintasks.argument_string(args)}')[1:]
        self.assertEqual(parsed, args)

    def test_a_string_that_does_not_parse_back_is_refused(self) -> None:
        with mock.patch.object(wintasks, "quote_argument", lambda value: value):
            with self.assertRaises(wintasks.ArgumentQuotingError):
                wintasks.argument_string(["--repo", "C:\\two words\\repo"])

    def test_the_same_agent_in_two_repositories_gets_two_tasks(self) -> None:
        here = wintasks.task_name(self.ROOT, "todo")
        there = wintasks.task_name(self.ROOT.with_name("other"), "todo")
        self.assertNotEqual(here, there)
        self.assertEqual(here, wintasks.task_name(self.ROOT, "todo"))

    def test_a_task_belongs_to_the_repository_that_named_it(self) -> None:
        leaf = wintasks.task_name(self.ROOT, "todo")
        self.assertEqual(wintasks.agent_of_task_name(leaf, self.ROOT), "todo")
        self.assertIsNone(
            wintasks.agent_of_task_name(leaf, self.ROOT.with_name("other")))
        self.assertIsNone(wintasks.agent_of_task_name("someone-elses-task",
                                                      self.ROOT))

    def test_a_name_that_cannot_be_a_task_name_is_refused(self) -> None:
        with self.assertRaises(wintasks.TaskError):
            wintasks.task_name(self.ROOT, "sneaky\\name")

    def test_every_task_lives_in_one_folder(self) -> None:
        self.assertTrue(
            wintasks.task_path(self.ROOT, "todo").startswith(
                wintasks.TASK_FOLDER + "\\"))

    def test_schedules_that_map_exactly_become_native_triggers(self) -> None:
        self.assertEqual(wintasks.translate("*/5 * * * *"),
                         [{"kind": "interval", "minutes": 5,
                           "anchor_minute": 0}])
        self.assertEqual(wintasks.translate("17 * * * *"),
                         [{"kind": "interval", "minutes": 60,
                           "anchor_minute": 17}])
        self.assertEqual(wintasks.translate("30 9 * * *"),
                         [{"kind": "daily", "hour": 9, "minute": 30}])
        self.assertEqual(wintasks.translate("@reboot"), [{"kind": "boot"}])

    def test_a_step_that_drifts_from_cron_is_covered_not_approximated(self) -> None:
        # Cron restarts */7 every hour, so no 7-minute repetition lands
        # on its minutes; the trigger has to be finer, never coarser.
        trigger = wintasks.translate("*/7 * * * *")[0]
        self.assertEqual(trigger, {"kind": "interval", "minutes": 1,
                                   "anchor_minute": 0})

    def test_a_calendar_schedule_keeps_its_native_shape(self) -> None:
        self.assertEqual(wintasks.translate("0 3 * * 0"),
                         [{"kind": "weekly", "weekday": 0, "hour": 3,
                           "minute": 0}])
        self.assertEqual(wintasks.translate("0 3 1 * *"),
                         [{"kind": "monthly", "day": 1, "hour": 3,
                           "minute": 0}])

    def test_a_coarse_trigger_covers_every_minute_the_schedule_names(self) -> None:
        for schedule in ("0 9-17 * * 1-5", "0,30 * * * *", "*/7 * * * *",
                         "5,20,41 2 3 4 *"):
            trigger = wintasks.translate(schedule)[0]
            step = int(trigger["minutes"])
            anchor = int(trigger["anchor_minute"])
            covered = set(range(anchor, 60, step))
            self.assertTrue(
                triggers.schedule_minutes(schedule) <= covered, schedule)

    def test_an_unreadable_schedule_is_refused_rather_than_guessed(self) -> None:
        for schedule in ("nonsense", "0 99 * * *", "* * * *"):
            with self.assertRaises(wintasks.ScheduleNotTranslatable):
                wintasks.translate(schedule)

    def test_the_first_firing_time_is_ahead_of_registration(self) -> None:
        # A start boundary in the past is a missed start Task Scheduler
        # would catch up on, which would run the agent at install time.
        now = datetime(2026, 7, 25, 17, 33, 12)
        for schedule in ("*/5 * * * *", "17 * * * *", "30 9 * * *"):
            trigger = wintasks.translate(schedule)[0]
            boundary = datetime.strptime(wintasks._boundary(trigger, now),
                                         "%Y-%m-%dT%H:%M:%S")
            self.assertGreater(boundary, now, schedule)

    def test_an_interval_lands_on_the_minutes_cron_would_have(self) -> None:
        now = datetime(2026, 7, 25, 17, 33, 12)
        trigger = wintasks.translate("*/5 * * * *")[0]
        self.assertEqual(wintasks._boundary(trigger, now),
                         "2026-07-25T17:35:00")

    def _document(self, root: Path | str = ROOT, command: str | None = None) -> str:
        return wintasks.build_task_xml(
            command=command or "C:\\tools\\agents-live.exe",
            arguments=wintasks.argument_string(
                ["--repo", str(root), "run", "--name", "todo", "--quiet"]),
            working_dir=str(root), schedules=("*/5 * * * *",),
            description="Agents Live: run agent 'todo'",
            uri=wintasks.task_path(root, "todo"), user_id="EXAMPLE\\dev",
            now=datetime(2026, 7, 25, 17, 33, 12))

    def test_a_task_pins_its_executable_and_working_directory(self) -> None:
        task = ET.fromstring(self._document().split("?>", 1)[1])
        namespace = {"t": wintasks._NS}
        action = task.find(".//t:Exec", namespace)
        self.assertEqual(action.findtext("t:Command", namespaces=namespace),
                         "C:\\tools\\agents-live.exe")
        self.assertEqual(
            action.findtext("t:WorkingDirectory", namespaces=namespace),
            str(self.ROOT))

    def test_a_repository_path_that_needs_escaping_survives_the_xml(self) -> None:
        root = "C:\\Users\\dev\\r&d <notes> \"one\""
        task = ET.fromstring(self._document(root).split("?>", 1)[1])
        namespace = {"t": wintasks._NS}
        action = task.find(".//t:Exec", namespace)
        self.assertEqual(
            action.findtext("t:WorkingDirectory", namespaces=namespace), root)
        arguments = action.findtext("t:Arguments", namespaces=namespace)
        self.assertEqual(
            wintasks.parse_command_line(f'"prog" {arguments}')[1:],
            ["--repo", root, "run", "--name", "todo", "--quiet"])

    def test_a_task_declares_the_encoding_the_scheduler_expects(self) -> None:
        self.assertTrue(self._document().startswith(
            '<?xml version="1.0" encoding="UTF-16"?>'))

    def test_a_registered_task_is_recognised_as_ours(self) -> None:
        self.assertTrue(wintasks._is_ours(self._document(), self.ROOT))

    def test_a_task_for_another_repository_is_not_ours(self) -> None:
        self.assertFalse(
            wintasks._is_ours(self._document(), self.ROOT.with_name("other")))

    def test_a_task_that_runs_something_else_is_not_ours(self) -> None:
        document = self._document(command="C:\\Windows\\System32\\cmd.exe")
        self.assertFalse(wintasks._is_ours(document, self.ROOT))

    def test_a_definition_that_did_not_decode_is_not_ours(self) -> None:
        # Read-back comes through the console code page; anything lossy
        # fails the check rather than being interpreted.
        self.assertFalse(
            wintasks._is_ours(self._document().replace("agents-live",
                                                       "agents-l\ufffdve"),
                              self.ROOT))

    def test_a_definition_that_is_not_a_task_is_not_ours(self) -> None:
        self.assertFalse(wintasks._is_ours("not xml at all", self.ROOT))

    def test_an_action_names_a_program_with_no_window_to_show(self) -> None:
        # A console program named directly in a task opens a console
        # window in the developer's session on every fire.
        host = "C:\\env\\Scripts\\pythonw.exe"
        with mock.patch.object(wintasks, "hidden_host",
                               return_value=Path(host)):
            command, arguments = wintasks.action_form(
                "C:\\tools\\agents-live.exe", ["run", "--name", "todo"])
        self.assertEqual(command, host)
        parsed = wintasks.parse_command_line(f'"prog" {arguments}')[1:]
        self.assertEqual(parsed, ["-P", "-m", "agents_live.hidden",
                                  "C:\\tools\\agents-live.exe",
                                  "run", "--name", "todo"])
        # Without an interpreter to hide behind, a visible window is
        # better than an agent that does not run.
        with mock.patch.object(wintasks, "hidden_host", return_value=None):
            self.assertEqual(
                wintasks.action_form("C:\\tools\\agents-live.exe", ["run"]),
                ("C:\\tools\\agents-live.exe", "run"))

    def test_ownership_looks_through_the_wrapper_to_what_runs(self) -> None:
        host = "C:\\env\\Scripts\\pythonw.exe"
        with mock.patch.object(wintasks, "hidden_host",
                               return_value=Path(host)):
            command, arguments = wintasks.action_form(
                "C:\\tools\\agents-live.exe",
                ["--repo", str(self.ROOT), "run", "--name", "todo"])
        document = wintasks.build_task_xml(
            command=command, arguments=arguments,
            working_dir=str(self.ROOT), schedules=("*/5 * * * *",),
            description="", uri=wintasks.task_path(self.ROOT, "todo"),
            user_id="EXAMPLE\\dev")
        self.assertTrue(wintasks._is_ours(document, self.ROOT))
        # The same interpreter running something else is not ours, and
        # neither is one running nothing at all.
        for foreign in (f'-P -m agents_live.hidden "C:\\evil.exe" run',
                        "-P -m other.module C:\\tools\\agents-live.exe",
                        "-P -m agents_live.hidden"):
            other = wintasks.build_task_xml(
                command=host, arguments=foreign,
                working_dir=str(self.ROOT), schedules=("*/5 * * * *",),
                description="", uri=wintasks.task_path(self.ROOT, "todo"),
                user_id="EXAMPLE\\dev")
            self.assertFalse(wintasks._is_ours(other, self.ROOT), foreign)

    def test_a_hidden_launch_reports_what_it_launched(self) -> None:
        # The scheduler reads an exit code, and there is no console to
        # explain anything on, so the status has to be the child's.
        with mock.patch.object(hidden.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["x"], 3)
            self.assertEqual(hidden.main(["x", "--flag"]), 3)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["x", "--flag"])
        self.assertEqual(hidden.main([]), 2)
        with mock.patch.object(hidden.subprocess, "run",
                               side_effect=OSError("no such program")):
            self.assertEqual(hidden.main(["missing"]), 127)

    def _clock_spec(self) -> triggers.TriggerSpec:
        return triggers.TriggerSpec(
            name="todo", kind=triggers.SCHEDULE, root=self.ROOT,
            schedules=("*/5 * * * *",),
            command=("C:\\tools\\agents-live.exe", "--repo", str(self.ROOT),
                     "run", "--name", "todo"),
            path="C:\\Windows\\System32")

    def test_a_task_that_reads_back_as_something_else_fails_the_write(
            self) -> None:
        # An update interrupted partway through leaves a store holding
        # something other than what was asked for. Saying so where it
        # happened beats reporting success and being surprised later.
        foreign = wintasks.build_task_xml(
            command="C:\\other.exe", arguments="", working_dir=str(self.ROOT),
            schedules=("*/5 * * * *",), description="", uri="x",
            user_id="EXAMPLE\\dev")
        with (
            mock.patch.object(wintasks, "read_definition",
                              side_effect=[None, foreign]),
            mock.patch.object(wintasks, "current_user_id",
                              return_value="EXAMPLE\\dev"),
            mock.patch.object(wintasks, "_run", return_value=(0, "", "")),
        ):
            with self.assertRaises(wintasks.TaskError):
                wintasks.install(self._clock_spec())

    def test_a_task_that_reads_back_firing_differently_fails_the_write(
            self) -> None:
        # The failure a normalized duration used to cause: written one
        # way, read back another, and rewritten by every later pass.
        spec = self._clock_spec()
        command, arguments = wintasks.action_form(spec.command[0],
                                                  list(spec.command[1:]))
        other = wintasks.build_task_xml(
            command=command, arguments=arguments, working_dir=str(self.ROOT),
            schedules=("0 9 * * *",), description="", uri="x",
            user_id="EXAMPLE\\dev")
        with (
            mock.patch.object(wintasks, "read_definition",
                              side_effect=[None, other]),
            mock.patch.object(wintasks, "current_user_id",
                              return_value="EXAMPLE\\dev"),
            mock.patch.object(wintasks, "_run", return_value=(0, "", "")),
        ):
            with self.assertRaises(wintasks.TaskError):
                wintasks.install(spec)

    def test_an_agent_is_registered_before_anything_is_removed(self) -> None:
        # Between the two an interruption can happen. Leaving an extra
        # task behind is visible and converges; leaving an agent with no
        # task at all is silence.
        spec = triggers.TriggerSpec(
            name="todo", kind=triggers.SCHEDULE, root=self.ROOT,
            schedules=("*/5 * * * *",),
            command=("C:\\tools\\agents-live.exe", "run", "--name", "todo"),
            path="C:\\Windows\\System32")
        order: list[str] = []
        registered: dict[str, object] = {}
        with (
            self._recording_task_store(registered),
            mock.patch.object(wintasks, "delete",
                              side_effect=lambda *_a, **kw: order.append(
                                  f"delete:{kw['kind']}")),
        ):
            with mock.patch.object(
                    wintasks, "_verify",
                    side_effect=lambda path, *_a: order.append("register")):
                wintasks.install(spec)
        self.assertEqual(order, ["register", f"delete:{wintasks.BOOT}"])

    def test_a_task_runs_as_the_user_who_registered_it(self) -> None:
        document = wintasks.build_task_xml(
            command="C:\\tools\\agents-live.exe", arguments="run",
            working_dir=str(self.ROOT), schedules=("*/5 * * * *",),
            description="", uri="x", user_id="EXAMPLE\\dev")
        self.assertEqual(wintasks.principal_of_definition(document),
                         ("EXAMPLE\\dev", wintasks.LOGON_TYPE))
        self.assertIsNone(wintasks.principal_of_definition("not xml"))

    def test_a_task_given_another_logon_type_is_reported(self) -> None:
        # An interactive token is what makes a scheduled agent run as the
        # developer, in their session. A task carrying a different one
        # runs under other rules, and that is not silent.
        tasks = [
            {"name": "todo@1", "command": "x", "arguments": "",
             "working_dir": str(self.ROOT),
             "principal": ("S-1-5-21-0", wintasks.LOGON_TYPE)},
            {"name": "other@2", "command": "x", "arguments": "",
             "working_dir": str(self.ROOT),
             "principal": ("S-1-5-21-0", "Password")},
        ]
        with (
            mock.patch.object(doctor.hostruntime, "native_scheduler",
                              return_value=doctor.hostruntime.TASK_SCHEDULER),
            mock.patch.object(wintasks, "registered_tasks",
                              return_value=tasks),
        ):
            ok, note = doctor._task_session_requirement()
        self.assertFalse(ok)
        self.assertIn("other@2", note)
        self.assertNotIn("todo@1", note)

    def test_the_signed_in_limit_is_stated_when_nothing_is_wrong(self) -> None:
        with (
            mock.patch.object(doctor.hostruntime, "native_scheduler",
                              return_value=doctor.hostruntime.TASK_SCHEDULER),
            mock.patch.object(wintasks, "registered_tasks", return_value=[]),
        ):
            ok, note = doctor._task_session_requirement()
        self.assertTrue(ok)
        self.assertIn("signed in", note)

    def test_a_watcher_respawn_registers_as_a_startup_task(self) -> None:
        spec = triggers.TriggerSpec(
            name="todo", kind=triggers.WATCHER, root=self.ROOT,
            schedules=("@reboot",),
            command=("C:\\tools\\agents-live.exe", "--repo", str(self.ROOT),
                     "internal", "ensure-watcher", "todo"),
            path="C:\\Windows\\System32")
        registered: dict[str, object] = {}
        with self._recording_task_store(registered):
            wintasks.install(spec)
        # A respawn is a startup trigger, and it never carries --boot:
        # its action is restarting a watcher, not running the agent.
        self.assertEqual(list(registered["schedules"]), ["@reboot"])
        self.assertNotIn("--boot", str(registered["arguments"]))
        self.assertTrue(str(registered["uri"]).endswith(wintasks.WATCH))

    def test_the_host_dispatches_the_watcher_respawn_it_can_persist(self) -> None:
        with (
            mock.patch.object(hostruntime, "native_scheduler",
                              return_value=hostruntime.TASK_SCHEDULER),
            mock.patch.object(headless, "repo_root", return_value=self.ROOT),
            mock.patch.object(headless, "watcher_spec") as spec,
            mock.patch.object(wintasks, "install", return_value="task") as install,
            mock.patch.object(wintasks, "delete", return_value=True) as delete,
            mock.patch.object(wintasks, "installed_names",
                              return_value=["todo"]) as installed,
        ):
            self.assertEqual(schedules.install_watcher_respawn("todo"), "task")
            self.assertTrue(schedules.remove_watcher_respawn("todo"))
            self.assertEqual(schedules.watcher_respawn_names(), ["todo"])
        spec.assert_called_once_with("todo")
        install.assert_called_once()
        delete.assert_called_once_with(self.ROOT, "todo", kind=wintasks.WATCH)
        installed.assert_called_once_with(self.ROOT, kind=wintasks.WATCH)

    def test_the_host_dispatches_to_the_scheduler_it_has(self) -> None:
        expected = (hostruntime.TASK_SCHEDULER
                    if sys.platform == "win32" else hostruntime.CRONTAB)
        self.assertEqual(hostruntime.native_scheduler(), expected)

    def test_scheduling_goes_where_the_host_keeps_schedules(self) -> None:
        spec = triggers.TriggerSpec(
            name="todo", kind=triggers.SCHEDULE, root=self.ROOT,
            schedules=("*/5 * * * *",),
            command=("C:\\tools\\agents-live.exe", "run"), path="")
        with (
            mock.patch.object(hostruntime, "native_scheduler",
                              return_value=hostruntime.TASK_SCHEDULER),
            mock.patch.object(wintasks, "install",
                              return_value="registered") as install,
        ):
            self.assertEqual(schedules.install(spec), "registered")
        install.assert_called_once_with(spec)

    def test_a_task_store_failure_reaches_the_command_layer(self) -> None:
        with (
            mock.patch.object(hostruntime, "native_scheduler",
                              return_value=hostruntime.TASK_SCHEDULER),
            mock.patch.object(wintasks, "remove",
                              side_effect=wintasks.TaskError("no")),
            mock.patch.object(headless, "repo_root", return_value=self.ROOT),
        ):
            with self.assertRaises(headless.AgentsLiveError):
                schedules.remove("todo")

    def test_each_way_an_agent_fires_gets_a_task_of_its_own(self) -> None:
        clock = wintasks.task_name(self.ROOT, "todo")
        boot = wintasks.task_name(self.ROOT, "todo", kind=wintasks.BOOT)
        watch = wintasks.task_name(self.ROOT, "todo", kind=wintasks.WATCH)
        self.assertEqual(len({clock, boot, watch}), 3)
        self.assertTrue(boot.endswith(wintasks.BOOT))
        self.assertTrue(watch.endswith(wintasks.WATCH))
        # All three still name the same agent, so enumeration and
        # removal reach them without knowing which is which.
        for name in (clock, boot, watch):
            self.assertEqual(wintasks.agent_of_task_name(name, self.ROOT),
                             "todo")

    def test_what_a_task_fires_on_survives_the_round_trip(self) -> None:
        # Convergence compares what a task fires on, and the only copy
        # of that is the registered document. If it cannot be read back
        # the same, every comparison reports a change that is not one.
        for schedules_ in (("*/5 * * * *",), ("30 9 * * *",),
                           ("0 6 * * 1",), ("0 3 15 * *",),
                           ("@reboot", "0 * * * *")):
            document = wintasks.build_task_xml(
                command="C:\\tools\\agents-live.exe", arguments="run",
                working_dir=str(self.ROOT), schedules=schedules_,
                description="", uri="\\AgentsLive\\todo",
                user_id="EXAMPLE\\dev",
                now=datetime(2026, 7, 25, 17, 33, 12))
            self.assertEqual(wintasks._definition_signature(document),
                             wintasks.trigger_signature(schedules_),
                             schedules_)

    def test_a_normalized_duration_reads_back_as_the_same_interval(self) -> None:
        # The store rewrites the duration it is given: PT60M is read
        # back as PT1H. Matched only as minutes, an hourly repetition
        # reads as a task that lost it, and convergence then rewrites
        # the task on every pass, forever.
        self.assertEqual(wintasks._interval_minutes("PT1H"), 60)
        self.assertEqual(wintasks._interval_minutes("PT60M"), 60)
        self.assertEqual(wintasks._interval_minutes("PT1H30M"), 90)
        self.assertEqual(wintasks._interval_minutes("P1DT"), 1440)
        self.assertIsNone(wintasks._interval_minutes("PT0M"))
        self.assertIsNone(wintasks._interval_minutes("every hour"))
        document = wintasks.build_task_xml(
            command="C:\\tools\\agents-live.exe", arguments="run",
            working_dir=str(self.ROOT), schedules=("0 * * * *",),
            description="", uri="\\AgentsLive\\todo",
            user_id="EXAMPLE\\dev", now=datetime(2026, 7, 25, 17, 33, 12))
        self.assertEqual(
            wintasks._definition_signature(document.replace("PT60M", "PT1H")),
            wintasks.trigger_signature(("0 * * * *",)))

    def test_the_loop_registers_a_task_that_belongs_to_no_agent(self) -> None:
        # The loop prunes tasks whose agent file is gone, and nothing in
        # a project defines the loop. Registered as an agent's task, it
        # would delete itself on its own first pass.
        name = wintasks.task_name(self.ROOT, "maintenance",
                                  kind=wintasks.HOST)
        self.assertIsNone(wintasks.agent_of_task_name(name, self.ROOT))
        spec = self._maintenance_spec()
        self.assertEqual(wintasks.kinds(spec), (wintasks.HOST,))
        # And its root is the tool's state directory, not a project the
        # host loop should sweep.
        with mock.patch.object(
                schedules.hostruntime, "native_scheduler",
                return_value=schedules.hostruntime.TASK_SCHEDULER), \
            mock.patch.object(
                wintasks, "registered_tasks",
                return_value=[{"name": name, "command": "agents-live.exe",
                               "arguments": "internal maintain",
                               "working_dir": str(self.ROOT)}]):
            self.assertEqual(schedules.persisted_roots(), [])

    def test_the_maintenance_loop_is_one_task_that_never_says_boot(self) -> None:
        spec = triggers.TriggerSpec(
            name="maintenance", kind=triggers.MAINTENANCE, root=self.ROOT,
            schedules=("@reboot", "0 * * * *"),
            command=("C:\\tools\\agents-live.exe", "internal", "maintain",
                     "--quiet"),
            path="C:\\Windows\\System32")
        registered: dict[str, object] = {}
        with self._recording_task_store(registered):
            wintasks.install(spec)
        # The loop does the same work at startup and on the hour, so one
        # task carries both triggers and no --boot tells them apart.
        self.assertEqual(list(registered["schedules"]),
                         ["@reboot", "0 * * * *"])
        self.assertNotIn("--boot", str(registered["arguments"]))
        self.assertFalse(str(registered["uri"]).endswith(wintasks.BOOT))
        # Host-scoped: it names no repository to work on.
        self.assertNotIn("--repo", str(registered["arguments"]))

    def _maintenance_spec(self) -> triggers.TriggerSpec:
        return triggers.TriggerSpec(
            name="maintenance", kind=triggers.MAINTENANCE, root=self.ROOT,
            schedules=("@reboot", "0 * * * *"),
            command=("C:\\tools\\agents-live.exe", "internal", "maintain",
                     "--quiet"),
            path="C:\\Windows\\System32")

    def test_the_loop_installs_where_the_host_keeps_schedules(self) -> None:
        spec = self._maintenance_spec()
        store: dict[str, object] = {"host": None}

        def registered_form(root, name, *, kind):
            return store["host"] if kind == wintasks.HOST else None

        with (
            mock.patch.object(hostruntime, "native_scheduler",
                              return_value=hostruntime.TASK_SCHEDULER),
            mock.patch.object(wintasks, "install", return_value="task") as install,
            mock.patch.object(wintasks, "registered_form", registered_form),
        ):
            # Not installed and not opting in: never adds the loop.
            self.assertFalse(
                schedules.install_maintenance(spec, install=False))
            install.assert_not_called()
            # Opting in registers it; once registered, nothing changes.
            self.assertTrue(schedules.install_maintenance(spec))
            install.assert_called_once_with(spec)
            store["host"] = wintasks.desired_form(spec, kind=wintasks.HOST)
            self.assertFalse(schedules.install_maintenance(spec))

    def test_the_loop_is_withdrawn_from_the_store_that_holds_it(self) -> None:
        spec = self._maintenance_spec()
        with (
            mock.patch.object(hostruntime, "native_scheduler",
                              return_value=hostruntime.TASK_SCHEDULER),
            mock.patch.object(wintasks, "delete", return_value=True) as delete,
        ):
            self.assertTrue(schedules.remove_maintenance(spec))
        delete.assert_called_once_with(self.ROOT, "maintenance",
                                       kind=wintasks.HOST)

    def test_convergence_compares_what_the_store_would_run(self) -> None:
        spec = triggers.TriggerSpec(
            name="todo", kind=triggers.SCHEDULE, root=self.ROOT,
            schedules=("*/5 * * * *",),
            command=("C:\\tools\\agents-live.exe", "--repo", str(self.ROOT),
                     "run", "--name", "todo", "--quiet"),
            path="")
        stale = ("C:\\old\\agents-live.exe", "run --name todo",
                 wintasks.trigger_signature(("*/5 * * * *",)))
        with (
            mock.patch.object(hostruntime, "native_scheduler",
                              return_value=hostruntime.TASK_SCHEDULER),
            mock.patch.object(wintasks, "registered_form",
                              side_effect=[stale, None, None]),
        ):
            old, new = schedules.current_form(spec)
        self.assertEqual(len(old), 1)
        self.assertEqual(len(new), 1)
        self.assertNotEqual(old, new)
        self.assertIn("C:\\old\\agents-live.exe", old[0])
        self.assertIn("C:\\tools\\agents-live.exe", new[0])

    def test_a_schedule_that_changed_is_a_change_to_converge(self) -> None:
        # The action is identical; only the firing times moved. A
        # comparison that looked at the command alone would miss it.
        spec = triggers.TriggerSpec(
            name="todo", kind=triggers.SCHEDULE, root=self.ROOT,
            schedules=("0 9 * * *",),
            command=("C:\\tools\\agents-live.exe", "run"), path="")
        registered = ("C:\\tools\\agents-live.exe", "run",
                      wintasks.trigger_signature(("0 10 * * *",)))
        with (
            mock.patch.object(hostruntime, "native_scheduler",
                              return_value=hostruntime.TASK_SCHEDULER),
            mock.patch.object(wintasks, "registered_form",
                              side_effect=[registered, None, None]),
        ):
            old, new = schedules.current_form(spec)
        self.assertNotEqual(old, new)

    def test_the_store_names_every_repository_it_still_runs(self) -> None:
        tasks = [
            {"name": "todo@abc", "command": "C:\\tools\\agents-live.exe",
             "arguments": "run", "working_dir": str(self.ROOT)},
            {"name": "gone@def", "command": "C:\\tools\\agents-live.exe",
             "arguments": "run", "working_dir": "C:\\repo\\gone"},
        ]
        with (
            mock.patch.object(hostruntime, "native_scheduler",
                              return_value=hostruntime.TASK_SCHEDULER),
            mock.patch.object(wintasks, "registered_tasks", return_value=tasks),
            mock.patch.object(Path, "is_dir", return_value=True),
        ):
            roots = schedules.persisted_roots()
        self.assertEqual([str(root) for root in roots],
                         [str(self.ROOT), "C:\\repo\\gone"])

    def test_the_task_sweep_takes_only_the_installation_being_removed(
            self) -> None:
        # Uninstall reaches tasks for projects it was never run from, so
        # ownership is what the action runs, not the root the name
        # digests. A task aimed at a source checkout keeps working after
        # the tool goes and is left registered (#219).
        environment = Path("C:\\uv\\tools\\agents-live")
        shim = "C:\\uv\\tools\\agents-live\\Scripts\\agents-live.exe"
        tasks = [
            {"name": "todo@abc", "command": shim, "arguments": "run",
             "working_dir": "C:\\repo\\a"},
            {"name": "gone@def", "command": shim, "arguments": "watch",
             "working_dir": "C:\\repo\\gone"},
            {"name": "dev@ghi", "command": "C:\\src\\agents-live.exe",
             "arguments": "run", "working_dir": "C:\\src"},
        ]
        calls: list[list[str]] = []
        with (
            mock.patch.object(wintasks, "registered_tasks", return_value=tasks),
            mock.patch.object(wintasks, "_run",
                              side_effect=lambda args, **kw: (
                                  calls.append(list(args)) or (0, "", ""))),
        ):
            removed = wintasks.remove_under(environment)
        self.assertEqual(removed, 2)
        self.assertEqual([call[2] for call in calls],
                         ["\\AgentsLive\\todo@abc", "\\AgentsLive\\gone@def"])

    def test_doctor_names_the_mechanism_this_host_dispatches_with(self) -> None:
        # The capability is the same question on both hosts; only the
        # program that answers it differs, and an apt line helps nobody
        # on Windows. The row is selected by the mechanism each module
        # reports, which is the same answer the runtime acts on.
        with (
            mock.patch.object(hostruntime, "native_scheduler",
                              return_value=hostruntime.TASK_SCHEDULER),
            mock.patch.object(watchsource, "mechanism",
                              return_value=watchsource.DIRECTORY_CHANGES),
            mock.patch.object(doctor.preflight, "check", return_value=None),
        ):
            schedule = doctor._mechanism_check("schedule")
            watch = doctor._mechanism_check("watch")
        self.assertEqual(schedule[0], "Task Scheduler")
        self.assertEqual(watch[0], "directory change notification")
        self.assertNotIn("apt", schedule[3])
        self.assertNotIn("apt", watch[3])
        with (
            mock.patch.object(hostruntime, "native_scheduler",
                              return_value=hostruntime.CRONTAB),
            mock.patch.object(watchsource, "mechanism",
                              return_value=watchsource.INOTIFY),
            mock.patch.object(doctor.preflight, "check", return_value=None),
        ):
            self.assertEqual(doctor._mechanism_check("schedule")[0], "crontab")
            self.assertEqual(doctor._mechanism_check("watch")[0], "inotifywait")

    def test_doctor_reports_tasks_no_run_of_this_tool_can_reach(self) -> None:
        gone = "C:\\repo\\deleted"
        tasks = [
            {"name": wintasks.task_name(self.ROOT, "lost"),
             "command": "C:\\tools\\agents-live.exe", "arguments": "run",
             "working_dir": str(self.ROOT)},
            {"name": "orphan@deadbeef",
             "command": "C:\\tools\\agents-live.exe", "arguments": "run",
             "working_dir": gone},
        ]
        with (
            mock.patch.object(hostruntime, "native_scheduler",
                              return_value=hostruntime.TASK_SCHEDULER),
            mock.patch.object(doctor, "REPO", self.ROOT),
            mock.patch.object(wintasks, "registered_tasks", return_value=tasks),
            mock.patch.object(headless, "list_agents", return_value=[]),
        ):
            orphans, stale = doctor._trigger_inconsistencies()
        # A task for this project whose agent file is gone is an orphan;
        # a task pinned to a root that no longer exists can never be
        # matched by name again, so it surfaces here or nowhere.
        self.assertEqual(orphans, ["lost"])
        self.assertEqual(stale, [f"{gone} (project root moved or deleted)"])

    def test_convergence_reads_the_store_the_host_writes(self) -> None:
        spec = triggers.TriggerSpec(
            name="todo", kind=triggers.SCHEDULE, root=self.ROOT,
            schedules=("*/5 * * * *",),
            command=("C:\\tools\\agents-live.exe", "run"), path="")
        with (
            mock.patch.object(hostruntime, "native_scheduler",
                              return_value=hostruntime.TASK_SCHEDULER),
            mock.patch.object(schedules, "installed_names",
                              return_value=["todo", "deleted"]),
            mock.patch.object(schedules, "watcher_respawn_names",
                              return_value=[]),
            mock.patch.object(migrate, "agent_file_exists",
                              side_effect=lambda name: name == "todo"),
            mock.patch.object(migrate, "schedule_spec", return_value=spec),
            mock.patch.object(schedules, "current_form",
                              return_value=(["old"], ["new"])),
        ):
            plan = migrate.plan_task_migration()
        self.assertEqual(plan["schedule"], {"todo": (["old"], ["new"])})
        self.assertEqual(plan["missing"], ["deleted"])

    def test_adoption_is_refused_where_a_name_carries_its_root(self) -> None:
        # A task is found by a name digesting its root, so entries from
        # a root that no longer exists cannot be looked up at all.
        with (
            mock.patch.object(hostruntime, "native_scheduler",
                              return_value=hostruntime.TASK_SCHEDULER),
            mock.patch.object(sys, "argv",
                              ["agents-live internal migrate",
                               "--adopt", "C:\\repo\\gone"]),
        ):
            with self.assertRaises(headless.AgentsLiveError) as raised:
                migrate.main()
        self.assertIn("--adopt is not available", str(raised.exception))


class TestScheduleLanguage(unittest.TestCase):
    """Reading a cron expression, which both hosts now depend on."""

    def test_a_moment_the_expression_names_is_a_firing_time(self) -> None:
        moment = datetime(2026, 7, 27, 9, 30)  # a Monday
        self.assertTrue(triggers.schedule_matches("30 9 * * *", moment))
        self.assertTrue(triggers.schedule_matches("*/15 * * * *", moment))
        self.assertTrue(triggers.schedule_matches("30 9-17 * * 1-5", moment))
        self.assertFalse(triggers.schedule_matches("31 9 * * *", moment))
        self.assertFalse(triggers.schedule_matches("30 9 * * 0", moment))

    def test_a_restricted_day_and_weekday_are_ored_as_cron_does(self) -> None:
        # Cron's one surprise: with both restricted, either can fire.
        monday = datetime(2026, 7, 27, 0, 0)
        self.assertTrue(triggers.schedule_matches("0 0 1 * 1", monday))
        self.assertTrue(triggers.schedule_matches("0 0 27 * 5", monday))
        self.assertFalse(triggers.schedule_matches("0 0 1 * 5", monday))

    def test_sunday_has_one_spelling(self) -> None:
        sunday = datetime(2026, 7, 26, 6, 0)
        self.assertTrue(triggers.schedule_matches("0 6 * * 7", sunday))
        self.assertTrue(triggers.schedule_matches("0 6 * * 0", sunday))

    def test_an_expression_this_project_cannot_read_says_so(self) -> None:
        for expression in ("", "* * * *", "0 24 * * *", "*/0 * * * *",
                           "0 0 * * x"):
            with self.assertRaises(triggers.ScheduleSyntaxError):
                triggers.schedule_matches(expression, datetime(2026, 7, 27))


class TestDueness(_TempProject):
    """Whether a wake is a real firing time, where the host is coarse."""

    SCHEDULE = ("30 9 * * *",)

    def _windows(self):
        return mock.patch.object(hostruntime, "native_scheduler",
                                 return_value=hostruntime.TASK_SCHEDULER)

    def test_a_declined_fire_says_so_on_a_hand_run(self) -> None:
        # The skip reached the structured log and nothing else, so a
        # scheduled agent run by hand outside its firing minute printed
        # nothing and exited 0, which reads as a completed run (#187).
        code, out = self._decline()
        self.assertEqual(code, 0)
        self.assertIn("is not due", out)
        self.assertIn(TEST_CRON_SCHEDULE, out)

    def test_a_declined_fire_stays_silent_when_quiet(self) -> None:
        # Every persisted scheduled invocation carries --quiet, so cron
        # mail and Task Scheduler see exactly what they saw before.
        code, out = self._decline("--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_a_legacy_windows_task_repairs_and_skips_before_dispatch(
            self) -> None:
        self.write_agent("demo", AGENT_DEFINITION.replace(
            "pre-processor: Agents/handlers/prep.py\n", ""))
        with (
            mock.patch.object(schedules, "repair_legacy_clock",
                              return_value=True),
            mock.patch.object(headless, "headless_agent") as dispatch,
            mock.patch.object(sys, "argv", ["run.py", "--name", "demo"]),
            mock.patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            self.assertEqual(run.main(), 0)
        dispatch.assert_not_called()
        self.assertIn("repaired the legacy scheduled task", out.getvalue())

    def test_repairing_a_legacy_clock_rewrites_the_registered_action(
            self) -> None:
        self.write_agent("demo", AGENT_DEFINITION.replace(
            "pre-processor: Agents/handlers/prep.py\n", ""))
        old = (r"C:\tools\agents-live.exe",
               "--repo C:\\project run --name demo --quiet", [])
        with (
            self._windows(),
            mock.patch.object(wintasks, "registered_form", return_value=old),
            mock.patch.object(wintasks, "install",
                              return_value="registered") as install,
        ):
            self.assertTrue(schedules.repair_legacy_clock("demo"))
        spec = install.call_args.args[0]
        self.assertIn("--scheduled", spec.command)

    def _decline(self, *extra: str):
        self.write_agent("demo", AGENT_DEFINITION.replace(
            "pre-processor: Agents/handlers/prep.py\n", ""))
        with (
            mock.patch.object(schedules, "claim_due_minute",
                              return_value=False),
            mock.patch.object(
                sys, "argv",
                ["run.py", "--name", "demo", "--scheduled", *extra]),
            mock.patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            return run.main(), out.getvalue()

    def test_a_crontab_host_never_second_guesses_its_own_scheduler(self) -> None:
        # Cron fires only at firing times; asking again would be a way
        # to disagree with it, not a safeguard.
        with mock.patch.object(hostruntime, "native_scheduler",
                               return_value=hostruntime.CRONTAB):
            self.assertTrue(schedules.claim_due_minute(
                "todo", self.SCHEDULE, moment=datetime(2026, 7, 27, 11, 4)))

    def test_a_fire_at_a_firing_time_runs(self) -> None:
        with self._windows():
            self.assertTrue(schedules.claim_due_minute(
                "todo", self.SCHEDULE, moment=datetime(2026, 7, 27, 9, 30)))

    def test_a_fire_the_schedule_did_not_name_is_declined(self) -> None:
        with self._windows():
            self.assertFalse(schedules.claim_due_minute(
                "todo", self.SCHEDULE, moment=datetime(2026, 7, 27, 9, 31)))

    def test_a_second_fire_in_the_same_minute_runs_once(self) -> None:
        moment = datetime(2026, 7, 27, 9, 30)
        with self._windows():
            self.assertTrue(
                schedules.claim_due_minute("todo", self.SCHEDULE, moment=moment))
            self.assertFalse(
                schedules.claim_due_minute("todo", self.SCHEDULE,
                                           moment=moment.replace(second=40)))
            self.assertTrue(
                schedules.claim_due_minute("todo", self.SCHEDULE,
                                           moment=moment + timedelta(days=1)))

    def test_one_agent_claiming_a_minute_does_not_claim_it_for_another(self) -> None:
        moment = datetime(2026, 7, 27, 9, 30)
        with self._windows():
            self.assertTrue(
                schedules.claim_due_minute("todo", self.SCHEDULE, moment=moment))
            self.assertTrue(
                schedules.claim_due_minute("other", self.SCHEDULE, moment=moment))

    def test_any_of_an_agents_schedules_can_make_it_due(self) -> None:
        with self._windows():
            self.assertTrue(schedules.claim_due_minute(
                "todo", ("@reboot", "0 * * * *"),
                moment=datetime(2026, 7, 27, 11, 0)))


class TestWindowsHeartbeat(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.shim = self.home / ".local" / "bin" / "agents-live"
        self.shim.parent.mkdir(parents=True)
        self.shim.write_text("#!/bin/sh\n", encoding="utf-8")
        self.shim.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            # USERPROFILE too: Path.home() reads HOME on POSIX and
            # USERPROFILE on Windows, and this fixture has to redirect
            # the home directory on whichever host is running it.
            "USERPROFILE": str(self.home),
            "XDG_STATE_HOME": str(self.state),
            "WSL_DISTRO_NAME": "Ubuntu",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_execution_uses_shared_state_outside_projects(self) -> None:
        with mock.patch.object(heartbeat.subprocess, "run"):
            self.assertEqual(heartbeat.run_once(), 0)
        self.assertTrue((self.state / "agents-live" / "heartbeat.ok").is_file())
        self.assertTrue((self.state / "agents-live" / "heartbeat.log").is_file())

    def test_install_migrates_legacy_only_after_fresh_beacon(self) -> None:
        with (
            mock.patch.object(heartbeat, "_task_exists", return_value=True),
            mock.patch.object(heartbeat, "_register_task") as register,
            mock.patch.object(heartbeat, "_start_task") as start,
            mock.patch.object(heartbeat, "_wait_for_fresh_beacon",
                              return_value=True),
            mock.patch.object(heartbeat, "_unregister_task") as unregister,
        ):
            heartbeat.install()
        register.assert_called_once_with("Ubuntu", self.shim)
        start.assert_called_once_with("Agents Live Heartbeat (Ubuntu)")
        unregister.assert_called_once_with("WSL Heartbeat")

    def test_task_identity_is_scoped_per_distro(self) -> None:
        self.assertEqual(
            heartbeat.task_name("Ubuntu"), "Agents Live Heartbeat (Ubuntu)")
        self.assertEqual(
            heartbeat.task_name("Debian"), "Agents Live Heartbeat (Debian)")

    def test_best_effort_install_registers_only_on_wsl(self) -> None:
        with (
            mock.patch.object(heartbeat.hostruntime, "id",
                              return_value=heartbeat.hostruntime.WSL),
            mock.patch.object(heartbeat, "install") as install,
        ):
            self.assertTrue(heartbeat.install_best_effort("init"))
        install.assert_called_once_with()
        for runtime in (heartbeat.hostruntime.LINUX, heartbeat.hostruntime.WINDOWS):
            with (
                mock.patch.object(heartbeat.hostruntime, "id",
                                  return_value=runtime),
                mock.patch.object(heartbeat, "install") as install,
            ):
                self.assertFalse(heartbeat.install_best_effort("init"))
            install.assert_not_called()

    def test_best_effort_install_reports_failure_without_raising(self) -> None:
        with (
            mock.patch.object(heartbeat.hostruntime, "id",
                              return_value=heartbeat.hostruntime.WSL),
            mock.patch.object(
                heartbeat, "install",
                side_effect=RuntimeError("Windows PowerShell interop is "
                                         "unavailable")),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertFalse(heartbeat.install_best_effort("init"))
        message = stderr.getvalue()
        self.assertIn("could not register the Windows heartbeat during init",
                      message)
        self.assertIn("agents-live heartbeat install", message)

    def test_failed_migration_preserves_legacy_task(self) -> None:
        with (
            mock.patch.object(heartbeat, "_task_exists", return_value=True),
            mock.patch.object(heartbeat, "_register_task"),
            mock.patch.object(heartbeat, "_start_task"),
            mock.patch.object(heartbeat, "_wait_for_fresh_beacon",
                              return_value=False),
            mock.patch.object(heartbeat, "_unregister_task") as unregister,
            self.assertRaisesRegex(RuntimeError, "left unchanged"),
        ):
            heartbeat.install()
        unregister.assert_not_called()

    def test_install_uninstall_round_trip_targets_same_distro_task(self) -> None:
        with (
            mock.patch.object(
                heartbeat, "_task_exists", side_effect=[False, True]),
            mock.patch.object(heartbeat, "_register_task"),
            mock.patch.object(heartbeat, "_start_task"),
            mock.patch.object(heartbeat, "_wait_for_fresh_beacon",
                              return_value=True),
            mock.patch.object(heartbeat, "_unregister_task") as unregister,
        ):
            heartbeat.install("Ubuntu")
            heartbeat.uninstall("Ubuntu", retain_state=True)
        unregister.assert_called_once_with("Agents Live Heartbeat (Ubuntu)")

    def test_uninstall_removes_generated_state_only(self) -> None:
        directory = heartbeat.state_dir()
        directory.mkdir(parents=True)
        heartbeat.beacon_path().write_text("alive\n", encoding="utf-8")
        (directory / "heartbeat.log").write_text("log\n", encoding="utf-8")
        unrelated = directory / "unrelated.json"
        unrelated.write_text("{}\n", encoding="utf-8")
        with (
            mock.patch.object(heartbeat, "_task_exists", return_value=True),
            mock.patch.object(heartbeat, "_unregister_task") as unregister,
        ):
            heartbeat.uninstall()
        unregister.assert_called_once_with("Agents Live Heartbeat (Ubuntu)")
        self.assertTrue(unrelated.is_file())
        self.assertFalse(heartbeat.beacon_path().exists())

    def test_retain_state_keeps_generated_files(self) -> None:
        directory = heartbeat.state_dir()
        directory.mkdir(parents=True)
        heartbeat.beacon_path().write_text("alive\n", encoding="utf-8")
        with mock.patch.object(heartbeat, "_task_exists", return_value=False):
            heartbeat.uninstall(retain_state=True)
        self.assertTrue(heartbeat.beacon_path().is_file())

    def test_tool_uninstall_stops_when_host_cleanup_fails(self) -> None:
        with (
            mock.patch.object(uninstall, "_stop_own_watchers", return_value=[]),
            mock.patch.object(uninstall, "_tool_environment",
                              return_value=None),
            mock.patch.object(hostruntime, "id",
                              return_value=hostruntime.WSL),
            mock.patch.object(
                heartbeat, "uninstall", side_effect=RuntimeError("denied")),
            mock.patch.object(uninstall.subprocess, "run") as uv_uninstall,
            mock.patch("sys.stderr", io.StringIO()) as stderr,
        ):
            self.assertEqual(uninstall.main(["--distro", "Ubuntu"]), 1)
        uv_uninstall.assert_not_called()
        self.assertIn("uvx agents-live heartbeat uninstall", stderr.getvalue())

    def test_tool_uninstall_skips_host_cleanup_off_wsl(self) -> None:
        completed = subprocess.CompletedProcess(["uv"], 0)
        with (
            mock.patch.object(uninstall, "_stop_own_watchers", return_value=[]),
            mock.patch.object(uninstall, "_tool_environment",
                              return_value=None),
            mock.patch.object(hostruntime, "id",
                              return_value=hostruntime.LINUX),
            mock.patch.object(heartbeat, "uninstall") as host_cleanup,
            mock.patch.object(uninstall.health_check,
                              "remove_health_cron_lines",
                              return_value=True) as remove_loop,
            mock.patch.object(uninstall, "find_uv",
                              return_value="/usr/bin/uv"),
            mock.patch.object(uninstall.subprocess, "run",
                              return_value=completed) as uv_uninstall,
            mock.patch("sys.stdout", io.StringIO()),
            mock.patch("sys.stderr", io.StringIO()),
        ):
            self.assertEqual(uninstall.main([]), 0)
        host_cleanup.assert_not_called()
        remove_loop.assert_called_once_with()
        uv_uninstall.assert_called_once_with(
            ["/usr/bin/uv", "tool", "uninstall", "agents-live"], check=False)

    def test_windows_tool_uninstall_waits_until_its_own_processes_exit(
            self) -> None:
        environment = Path(r"C:\uv\tools\agents-live")
        with (
            mock.patch.object(uninstall, "_stop_own_watchers", return_value=[]),
            mock.patch.object(hostruntime, "id",
                              return_value=hostruntime.WINDOWS),
            mock.patch.object(uninstall, "_sweep_triggers"),
            mock.patch.object(uninstall.health_check,
                              "remove_health_cron_lines",
                              return_value=False),
            mock.patch.object(uninstall.completions, "remove",
                              return_value=[]),
            mock.patch.object(uninstall, "find_uv", return_value="uv.exe"),
            mock.patch.object(uninstall, "_tool_environment",
                              return_value=environment),
            mock.patch.object(uninstall, "_handoff_windows_uninstall",
                              return_value=True) as handoff,
            mock.patch.object(uninstall.subprocess, "run") as direct_uninstall,
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(uninstall.main([]), 0)
        handoff.assert_called_once_with("uv.exe", environment)
        direct_uninstall.assert_not_called()

    def test_windows_uninstall_helper_waits_for_the_tool_environment(
            self) -> None:
        environment = Path(r"C:\uv tools\agents-live")
        with (
            mock.patch.object(
                hostruntime, "defer_until_environment_exits",
                return_value=True) as defer,
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertTrue(uninstall._handoff_windows_uninstall(
                r"C:\uv\uv.exe", environment))
        defer.assert_called_once_with(
            [r"C:\uv\uv.exe", "tool", "uninstall", "agents-live"],
            environment)

    def test_install_refuses_cross_distro_target(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            heartbeat.install("Debian")

    def test_uninstall_stops_only_the_watchers_it_owns(self) -> None:
        # A watcher holds the executables uv deletes, so it fails the
        # removal after the host cleanup has already run (#219). Only
        # the ones running out of the tool environment are ours; a
        # watcher run from a checkout is somebody's working tree.
        environment = Path(self.home) / "uv" / "tools" / "agents-live"
        (environment / "Scripts").mkdir(parents=True)
        ours = str(environment / "Scripts" / "agents-live.exe")
        theirs = str(Path(self.home) / "src" / "agents-live" / "agents-live")
        listed = [
            (11, f"{ours} --repo /p internal watch-loop mine"),
            (22, f"{theirs} --repo /p internal watch-loop dev"),
        ]
        stopped: list[int] = []
        with (
            mock.patch.object(headless.hostruntime, "process_command_lines",
                              return_value=listed),
            mock.patch.object(uninstall.hostruntime, "terminate",
                              side_effect=lambda pid, **kw: stopped.append(pid)),
            mock.patch.object(uninstall.hostruntime, "is_alive",
                              return_value=False),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(uninstall._stop_own_watchers(environment), [])
        self.assertEqual(stopped, [11])

    def test_uninstall_removes_nothing_while_a_watcher_survives(self) -> None:
        # The point of checking first: what the removal cannot do must
        # leave a working installation, not a stripped host (#219).
        with (
            mock.patch.object(uninstall, "_stop_own_watchers",
                              return_value=[(11, "mine", "/p")]),
            mock.patch.object(uninstall, "_tool_environment",
                              return_value=None),
            mock.patch.object(heartbeat, "uninstall") as host_cleanup,
            mock.patch.object(uninstall.health_check,
                              "remove_health_cron_lines") as remove_loop,
            mock.patch.object(uninstall, "_sweep_triggers") as sweep,
            mock.patch.object(uninstall.subprocess, "run") as uv_uninstall,
            mock.patch("sys.stderr", io.StringIO()) as stderr,
        ):
            self.assertEqual(uninstall.main([]), 1)
        host_cleanup.assert_not_called()
        remove_loop.assert_not_called()
        sweep.assert_not_called()
        uv_uninstall.assert_not_called()
        self.assertIn("mine (pid 11)", stderr.getvalue())

    def test_cron_sweep_takes_only_entries_from_the_installation(self) -> None:
        # Root-agnostic on purpose: uninstall withdraws entries for
        # projects it was never run from. The executable is what proves
        # ownership, so a developer's checkout entry and an unrelated
        # user entry both survive (#219).
        # Text, not Path: a crontab is POSIX and its lines are parsed
        # with shlex, which would eat the backslashes a Windows Path
        # renders when this test runs there.
        where = "/home/dev/.local/share/uv/tools/agents-live"
        shim = f"{where}/bin/agents-live"
        checkout = "/home/dev/src/agents-live/agents-live"
        environment = Path(where)
        lines = [
            f"*/5 * * * * cd /a && {shim} --repo /a internal run --name one",
            f"0 * * * * cd /b && {shim} --repo /b internal run --name two",
            f"*/5 * * * * cd /c && {checkout} --repo /c internal run --name dev",
            "0 3 * * * /usr/bin/backup --nightly",
        ]
        written: list[list[str]] = []
        with (
            mock.patch.object(headless, "crontab_lock",
                              contextlib.nullcontext),
            mock.patch.object(headless, "current_crontab_lines",
                              return_value=lines),
            mock.patch.object(headless, "install_crontab",
                              side_effect=written.append),
        ):
            removed = headless.remove_cron_entries_under(environment)
        self.assertEqual(removed, 2)
        self.assertEqual(written, [[lines[2], lines[3]]])

    def test_cron_sweep_leaves_the_crontab_alone_when_nothing_is_ours(
            self) -> None:
        environment = Path("/home/dev/.local/share/uv/tools/agents-live")
        with (
            mock.patch.object(headless, "crontab_lock",
                              contextlib.nullcontext),
            mock.patch.object(headless, "current_crontab_lines",
                              return_value=["0 3 * * * /usr/bin/backup"]),
            mock.patch.object(headless, "install_crontab") as install,
        ):
            self.assertEqual(
                headless.remove_cron_entries_under(environment), 0)
        install.assert_not_called()

    def test_task_sweep_reads_task_actions_as_windows_paths(self) -> None:
        environment = Path(r"C:\uv\tools\agents-live")
        tasks = [{
            "name": "demo@12345678",
            "command": r"c:\UV\TOOLS\AGENTS-LIVE\Scripts\agents-live.exe",
            "arguments": "--repo C:\\project run --name demo --scheduled",
        }]
        with (
            mock.patch.object(wintasks, "registered_tasks",
                              return_value=tasks),
            mock.patch.object(wintasks, "_run",
                              return_value=(0, "", "")) as delete,
        ):
            self.assertEqual(wintasks.remove_under(environment), 1)
        delete.assert_called_once_with([
            "/Delete", "/TN", r"\AgentsLive\demo@12345678", "/F"])

    def test_uninstall_removes_crontab_lock(self) -> None:
        directory = heartbeat.state_dir()
        directory.mkdir(parents=True)
        (directory / "crontab.lock").touch()
        with mock.patch.object(heartbeat, "_task_exists", return_value=False):
            heartbeat.uninstall()
        self.assertFalse((directory / "crontab.lock").exists())

    def test_doctor_accepts_stable_distro_task(self) -> None:
        launcher = r"C:\Program Files\WSL\wslg.exe"
        execute, arguments = heartbeat.task_action(
            "Ubuntu", self.shim, launcher)
        # The launcher has to be the one Windows gives no console to.
        # Nothing else in the action may be a path on the Windows side:
        # the distro reaches its own shim directly.
        self.assertEqual(execute, launcher)
        head, _, linux = arguments.partition(" -- ")
        self.assertEqual(head, "-d Ubuntu")
        self.assertEqual(shlex.split(linux), [str(self.shim), "heartbeat"])
        task = {
            "Enabled": True,
            "Execute": execute,
            "Arguments": arguments,
            "Interval": "PT5M",
        }
        with mock.patch.object(
                heartbeat, "task_configuration", return_value=(task, False)), \
                mock.patch.object(
                    heartbeat, "windowless_launcher", return_value=launcher), \
                mock.patch.object(
                    heartbeat, "stable_cli_path", return_value=self.shim):
            self.assertEqual(
                doctor._windows_heartbeat_config(),
                (True, "enabled; distro Ubuntu; windowless stable CLI shim; "
                       "repeats every 5 min"))

    def test_doctor_flags_every_superseded_launcher(self) -> None:
        # Two shapes predate this one: wsl.exe named directly, which
        # showed a console every five minutes, and the VBScript wrapper
        # that hid it. Doctor names each and points at the same repair.
        launcher = r"C:\Program Files\WSL\wslg.exe"
        cases = {
            "wsl.exe": "showing a console",
            r"C:\Windows\System32\wscript.exe": "VBScript",
        }
        for execute, expected in cases.items():
            with self.subTest(execute=execute):
                task = {
                    "Enabled": True,
                    "Execute": execute,
                    "Arguments": "-d Ubuntu --exec /ignored heartbeat",
                    "Interval": "PT5M",
                }
                with mock.patch.object(
                        heartbeat, "task_configuration",
                        return_value=(task, False)), \
                        mock.patch.object(
                            heartbeat, "windowless_launcher",
                            return_value=launcher), \
                        mock.patch.object(
                            heartbeat, "stable_cli_path",
                            return_value=self.shim):
                    ok, note = doctor._windows_heartbeat_config()
                self.assertFalse(ok)
                self.assertIn(expected, note)
                self.assertIn("heartbeat install", note)

    def test_doctor_reports_a_launcher_it_cannot_find(self) -> None:
        # An unresolvable launcher is not a silently healthy heartbeat:
        # the reason has to reach the developer, because the repair is
        # on the Windows side and doctor is the only thing that says so.
        task = {
            "Enabled": True,
            "Execute": r"C:\Program Files\WSL\wslg.exe",
            "Arguments": "-d Ubuntu -- /ignored heartbeat",
            "Interval": "PT5M",
        }
        with mock.patch.object(
                heartbeat, "task_configuration", return_value=(task, False)), \
                mock.patch.object(
                    heartbeat, "windowless_launcher",
                    side_effect=RuntimeError("cannot find wslg.exe, run "
                                             "`wsl.exe --update`")):
            ok, note = doctor._windows_heartbeat_config()
        self.assertFalse(ok)
        self.assertIn("cannot find wslg.exe", note)
        self.assertIn("wsl.exe --update", note)

    def test_the_linux_half_of_the_action_is_quoted_for_a_shell(self) -> None:
        # wslg hands everything after -- to the distro's shell as
        # written, so a shim path with a space has to survive that shell
        # rather than the Windows parser that never sees it.
        shim = self.home / "my tools" / "agents-live"
        _, arguments = heartbeat.task_action("Ubuntu", shim, "wslg.exe")
        head, _, linux = arguments.partition(" -- ")
        self.assertEqual(head, "-d Ubuntu")
        self.assertEqual(shlex.split(linux), [str(shim), "heartbeat"])

    def test_a_distro_name_with_a_space_stays_one_argument(self) -> None:
        # The distro name is read by the Windows command-line parser,
        # the half of the string wslg keeps for itself.
        _, arguments = heartbeat.task_action(
            "My Distro", self.shim, "wslg.exe")
        self.assertTrue(arguments.startswith('-d "My Distro" -- '))

    def test_the_launcher_is_located_once_and_remembered(self) -> None:
        # Where WSL was installed does not change while the process
        # runs, and doctor asks for the answer on a path that already
        # spends two PowerShell round trips.
        found = subprocess.CompletedProcess(
            [], 0, stdout=" C:\\Program Files\\WSL\\wslg.exe \n", stderr="")
        with mock.patch.object(heartbeat, "_launcher", None), \
                mock.patch.object(
                    heartbeat, "_run_powershell",
                    return_value=found) as powershell:
            first = heartbeat.windowless_launcher()
            second = heartbeat.windowless_launcher()
        self.assertEqual(first, r"C:\Program Files\WSL\wslg.exe")
        self.assertEqual(second, first)
        self.assertEqual(powershell.call_count, 1)

    def test_a_host_without_the_launcher_says_how_to_get_it(self) -> None:
        # Silence would leave the developer with a heartbeat that never
        # installs and no idea that WSL itself is what needs updating.
        empty = subprocess.CompletedProcess([], 0, stdout="\n", stderr="")
        with mock.patch.object(heartbeat, "_launcher", None), \
                mock.patch.object(
                    heartbeat, "_run_powershell", return_value=empty):
            with self.assertRaises(RuntimeError) as caught:
                heartbeat.windowless_launcher()
        self.assertIn("wslg.exe", str(caught.exception))
        self.assertIn("wsl.exe --update", str(caught.exception))

    def test_doctor_recommends_migration_for_legacy_task(self) -> None:
        with mock.patch.object(
                heartbeat, "task_configuration", return_value=(None, True)):
            ok, note = doctor._windows_heartbeat_config()
        self.assertFalse(ok)
        self.assertIn("requires migration", note)

    @unittest.skipIf(
        sys.platform == "win32",
        "the wrapper is a POSIX script a WSL crontab runs inside the "
        "distro. Executing it through whatever bash a Windows PATH "
        "happens to offer tests that bash, not this wrapper.")
    def test_compatibility_wrapper_executes_automatic_migration(self) -> None:
        wrapper = Path(heartbeat.__file__).with_name("windows-heartbeat.sh")
        invocation = self.root / "invocation"
        # as_posix() so the redirect target is a path the shell understands
        # on a Windows host too, rather than a literal file name in its cwd.
        self.shim.write_text(
            f"#!/bin/sh\nprintf '%s\\n' \"$*\" > '{invocation.as_posix()}'\n",
            encoding="utf-8")
        completed = subprocess.run(
            ["bash", str(wrapper), "/ignored/legacy/repo"],
            env={"HOME": str(self.home), "WSL_DISTRO_NAME": "Ubuntu",
                 "PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            invocation.read_text(encoding="utf-8"),
            "heartbeat install --distro Ubuntu\n")

    @unittest.skipIf(
        sys.platform == "win32",
        "the wrapper is a POSIX script a WSL crontab runs inside the "
        "distro. Executing it through whatever bash a Windows PATH "
        "happens to offer tests that bash, not this wrapper.")
    def test_compatibility_wrapper_fails_clearly_without_stable_shim(self) -> None:
        wrapper = Path(heartbeat.__file__).with_name("windows-heartbeat.sh")
        self.shim.unlink()
        completed = subprocess.run(
            ["bash", str(wrapper)],
            env={"HOME": str(self.home), "WSL_DISTRO_NAME": "Ubuntu",
                 "PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("uv shim not found", completed.stderr)


class TestQlog(_TempProject):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        script = Path(headless.__file__).with_name("qlog.py")
        env = os.environ.copy()
        env["TZ"] = "America/Los_Angeles"
        return subprocess.run(
            ["uv", "run", "--script", str(script), "--all", *args],
            capture_output=True, text=True, timeout=120, env=env)

    def _write_rows(self, name: str, rows: list[dict]) -> None:
        log = headless.logs_root() / f"{name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8")

    def test_canonical_agent_writer_emits_utc_z_timestamp(self) -> None:
        log = headless.logs_root() / "canonical-writer.log"

        headless.log_event(log, phase="test", status="ok")

        row = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(row["log_schema"], 5)
        self.assertTrue(row["ts"].endswith("Z"))
        parsed = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        self.assertEqual(parsed.utcoffset(), timedelta(0))

    def test_time_filters_preserve_instants_and_independent_bounds(self) -> None:
        rows = [
            {"log_schema": 5, "ts": "2026-07-20T19:00:00Z",
             "agent_name": "before", "event_id": "before"},
            {"log_schema": 5, "ts": "2026-07-20T20:00:03Z",
             "agent_name": "zulu", "event_id": "zulu"},
            {"log_schema": 5, "ts": "2026-07-20T13:00:03-07:00",
             "agent_name": "offset", "event_id": "offset"},
            {"log_schema": 5, "ts": "2026-07-20T20:00:03",
             "agent_name": "legacy", "event_id": "legacy"},
            {"log_schema": 5, "ts": "2026-07-20T21:00:00+00:00",
             "agent_name": "after", "event_id": "after"},
        ]
        self._write_rows("time-formats", rows)

        since = self._run(
            "--since", "2026-07-20T20:00:03Z", "--format", "jsonl",
            "--columns", "ts,event_id", "--asc")
        until = self._run(
            "--until", "2026-07-20T20:00:03Z", "--format", "jsonl",
            "--columns", "ts,event_id", "--asc")
        bounded = self._run(
            "--since", "2026-07-20T19:30:00Z",
            "--until", "2026-07-20T20:30:00Z", "--format", "jsonl",
            "--columns", "ts,event_id", "--asc")

        for result in (since, until, bounded):
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [json.loads(line)["event_id"] for line in since.stdout.splitlines()],
            ["zulu", "offset", "legacy", "after"])
        self.assertEqual(
            [json.loads(line)["event_id"] for line in until.stdout.splitlines()],
            ["before"])
        bounded_rows = [json.loads(line) for line in bounded.stdout.splitlines()]
        self.assertEqual(
            [row["event_id"] for row in bounded_rows],
            ["zulu", "offset", "legacy"])
        self.assertTrue(all(row["ts"].endswith("+00:00")
                            for row in bounded_rows))

    def test_relative_since_uses_utc_instants(self) -> None:
        now = datetime.now(timezone.utc)
        self._write_rows("relative-time", [
            {"log_schema": 5,
             "ts": (now - timedelta(hours=9)).isoformat().replace("+00:00", "Z"),
             "agent_name": "stale", "event_id": "stale"},
            {"log_schema": 5,
             "ts": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
             "agent_name": "recent", "event_id": "recent"},
        ])

        result = self._run(
            "--since", "8h", "--format", "jsonl", "--columns", "event_id")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [json.loads(line)["event_id"] for line in result.stdout.splitlines()],
            ["recent"])

    def test_invalid_time_filter_returns_usage_error(self) -> None:
        self._write_rows("invalid-time", [{
            "log_schema": 5, "ts": "2026-07-20T20:00:03Z",
            "agent_name": "fixture", "event_id": "fixture",
        }])

        result = self._run("--until", "now")

        self.assertEqual(result.returncode, 2)
        self.assertIn("usage_error", result.stderr)
        self.assertIn("invalid timestamp 'now'", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class TestTimeline(_TempProject):
    def test_bare_timeline_keeps_valid_rows_among_invalid_rows(self) -> None:
        log = headless.logs_root() / "mixed.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"log_schema": 5, "ts": "2026-07-18T20:00:00Z",
             "agent_name": "valid-agent", "phase": "done", "status": "ok"},
            {"log_schema": 4, "ts": "2026-07-18T19:00:00Z",
             "agent_name": "legacy-agent"},
        ]
        log.write_text(
            "\n".join(
                [json.dumps(row) for row in rows]
                + ["not-json", "[]", json.dumps({
                    "log_schema": 5,
                    "ts": [],
                    "agent_name": "malformed-agent",
                })]
            ),
            encoding="utf-8",
        )

        result = subprocess.run(
            [str(headless.cli_shim_path()), "logs", "timeline"],
            capture_output=True,
            text=True,
            timeout=120,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Timeline (all agents, last 50)", result.stdout)
        self.assertIn("valid-agent", result.stdout)
        self.assertNotIn("legacy-agent", result.stdout)
        self.assertNotIn("malformed-agent", result.stdout)
        self.assertIn("skipped 4 malformed or pre-v5 rows", result.stderr)

    def test_flat_script_resolves_registered_default_repo(self) -> None:
        # Regression for #48: logs/timeline run via `uv run --script`,
        # where paths is a top-level module and `from . import repos`
        # has no parent package. The crash fires only on the
        # registry-default branch, which the rest of the suite never
        # reaches because it pins AGENTS_LIVE_REPO.
        log = paths.repo_state_dir(self.root) / "logs" / "solo.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({
            "log_schema": 5, "ts": "2026-07-18T20:00:00Z",
            "agent_name": "registry-agent", "phase": "done", "status": "ok",
        }) + "\n", encoding="utf-8")

        xdg = self.root / "xdg-config"
        (xdg / "agents-live").mkdir(parents=True)
        # json.dumps, not an f-string: a Windows path is full of
        # backslashes, and TOML reads those as escape sequences.
        (xdg / "agents-live" / "config.toml").write_text(
            'default_repo = "proj"\n\n[repos]\n'
            f'proj = {json.dumps(str(self.root))}\n',
            encoding="utf-8")

        env = {k: v for k, v in os.environ.items() if k != paths.ENV_VAR}
        env["XDG_CONFIG_HOME"] = str(xdg)
        script = Path(headless.__file__).with_name("timeline.py")
        with tempfile.TemporaryDirectory() as bare_cwd:
            result = subprocess.run(
                [shutil.which("uv") or "uv", "run", "--script",
                 str(script), "--all"],
                capture_output=True, text=True, timeout=120,
                cwd=bare_cwd, env=env,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ImportError", result.stderr)
        self.assertIn("registry-agent", result.stdout)

    def test_bare_subprocess_scripts_use_isolated_registry(self) -> None:
        log = paths.repo_state_dir(self.root) / "logs" / "solo.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({
            "log_schema": 5, "ts": "2026-07-18T20:00:00Z",
            "agent_name": "registry-agent", "phase": "done", "status": "ok",
        }) + "\n", encoding="utf-8")
        xdg = self.root / "isolated-config"
        (xdg / "agents-live").mkdir(parents=True)
        (xdg / "agents-live" / "config.toml").write_text(
            'default_repo = "proj"\n\n[repos]\n'
            f'proj = {json.dumps(str(self.root))}\n',
            encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if key != paths.ENV_VAR}
        env["XDG_CONFIG_HOME"] = str(xdg)
        scripts = Path(headless.__file__).parent
        with tempfile.TemporaryDirectory() as bare_cwd:
            query = subprocess.run(
                ["uv", "run", "--script", str(scripts / "qlog.py"),
                 "--all", "--format", "jsonl", "--limit", "1"],
                capture_output=True, text=True, timeout=120,
                cwd=bare_cwd, env=env)
            dashboard_help = subprocess.run(
                ["uv", "run", "--script", str(scripts / "dashboard.py"),
                 "--help"],
                capture_output=True, text=True, timeout=120,
                cwd=bare_cwd, env=env)
        self.assertEqual(query.returncode, 0, query.stderr)
        self.assertIn("registry-agent", query.stdout)
        self.assertEqual(
            dashboard_help.returncode, 0, dashboard_help.stderr)
        self.assertIn("--all-repos", dashboard_help.stdout)


class TestUpdateCheck(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
        })
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    @staticmethod
    def _response(metadata: dict) -> io.BytesIO:
        return io.BytesIO(json.dumps(metadata).encode())

    def test_refresh_selects_latest_stable_semantic_version(self) -> None:
        opener = mock.Mock(return_value=self._response({
            "info": {"version": "2.0.0rc1"},
            "releases": {"1.9.0": [], "2.0.0rc1": [], "1.10.0": []},
        }))
        result = update_check.refresh(now=100, opener=opener)
        self.assertEqual(result["latest_version"], "1.10.0")
        opener.assert_called_once()

    def test_cache_timestamp_controls_network_launch(self) -> None:
        self.assertEqual(update_check.CACHE_INTERVAL, 60 * 60)
        with mock.patch.object(update_check.hostruntime, "spawn_detached") as spawn:
            update_check.launch_if_stale(now=100)
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[0][2], update_check.__name__)
        self.assertEqual(spawn.call_args.kwargs["cwd"], Path.home())

        update_check.refresh(
            now=100,
            opener=mock.Mock(return_value=self._response({
                "info": {"version": "1.2.3"},
            })),
        )
        with mock.patch.object(update_check.hostruntime, "spawn_detached") as spawn:
            update_check.launch_if_stale(now=101)
        spawn.assert_not_called()

        with mock.patch.object(update_check.hostruntime, "spawn_detached") as spawn:
            update_check.launch_if_stale(now=100 + update_check.CACHE_INTERVAL - 1)
        spawn.assert_not_called()

        with mock.patch.object(update_check.hostruntime, "spawn_detached") as spawn:
            update_check.launch_if_stale(now=100 + update_check.CACHE_INTERVAL)
        spawn.assert_called_once()

    def test_legacy_opt_outs_do_not_suppress_check(self) -> None:
        config = Path(os.environ["XDG_CONFIG_HOME"]) / "agents-live" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("update_check = false\n", encoding="utf-8")
        with (
            mock.patch.dict(os.environ, {"AGENTS_LIVE_NO_UPDATE_CHECK": "1"}),
            mock.patch.object(update_check.hostruntime, "spawn_detached") as spawn,
        ):
            update_check.launch_if_stale(now=100)
        spawn.assert_called_once()

    def test_offline_and_malformed_metadata_are_cached_failures(self) -> None:
        offline = update_check.refresh(
            now=100, opener=mock.Mock(side_effect=TimeoutError))
        self.assertEqual(offline["error"], "TimeoutError")
        malformed = update_check.refresh(
            now=200,
            opener=mock.Mock(return_value=self._response({
                "info": {"version": "2.0.0rc1"},
                "releases": {"2.0.0beta1": [], "2.0.0rc1": []},
            })),
        )
        self.assertEqual(malformed["error"], "ValueError")
        self.assertIsNone(malformed["latest_version"])

    def test_malformed_cache_is_ignored(self) -> None:
        path = update_check.cache_path()
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(update_check.cached_result())

    def test_available_notice_is_emitted_once_per_release(self) -> None:
        update_check.refresh(
            now=100,
            opener=mock.Mock(return_value=self._response({
                "info": {"version": "1.2.3"},
            })),
        )
        notice = update_check.consume_notice("1.2.2", now=101)
        self.assertIn("agents-live upgrade", notice)
        self.assertIsNone(update_check.consume_notice("1.2.2", now=102))
        self.assertIsNone(update_check.consume_notice("1.2.3", now=102))
        # An hourly re-check that finds the SAME release must not
        # re-announce it: the notice is once per release, not per check.
        update_check.refresh(
            now=150,
            opener=mock.Mock(return_value=self._response({
                "info": {"version": "1.2.3"},
            })),
        )
        self.assertIsNone(update_check.consume_notice("1.2.2", now=151))
        # A genuinely new release announces again.
        update_check.refresh(
            now=200,
            opener=mock.Mock(return_value=self._response({
                "info": {"version": "1.2.4"},
            })),
        )
        self.assertIn(
            "1.2.4 is available",
            update_check.consume_notice("1.2.2", now=201),
        )

    def test_cli_suppresses_noninteractive_quiet_and_json_checks(self) -> None:
        with (
            mock.patch.object(update_check, "interactive", return_value=False),
            mock.patch.object(update_check, "consume_notice") as consume,
            mock.patch.object(update_check, "launch_if_stale") as launch,
        ):
            self.assertEqual(
                cli._finish(7, cli.COMMAND_BY_NAME["status"], [],
                            json_mode=False), 7)
            consume.assert_not_called()
            launch.assert_not_called()
        with (
            mock.patch.object(update_check, "interactive", return_value=True),
            mock.patch.object(update_check, "consume_notice") as consume,
            mock.patch.object(update_check, "launch_if_stale") as launch,
        ):
            cli._finish(0, cli.COMMAND_BY_NAME["run"], ["--quiet"],
                        json_mode=False)
            cli._finish(0, cli.COMMAND_BY_NAME["status"], [], json_mode=True)
            consume.assert_not_called()
            launch.assert_not_called()

class TestPipelineMcpStore(unittest.TestCase):
    """Store-level checks (no HTTP server started)."""

    def _tools(self):
        try:  # installed package layout
            from agents_live.pipeline_mcp import PipelineMcp
        except ImportError:  # flat checkout layout
            from pipeline_mcp import PipelineMcp
        server = PipelineMcp()
        app = server._build_app()
        put = app._tool_manager.get_tool("put").fn
        get = app._tool_manager.get_tool("get").fn
        return server, put, get

    def test_seeded_schemas_are_frozen_and_enforced(self) -> None:
        # PKG-001: the agent-facing put must never replace host-seeded
        # schema bindings, so agent output is always validated against
        # the schema the host chose.
        server, put, get = self._tools()
        schema = {
            "type": "object",
            "required": ["done"],
            "additionalProperties": False,
            "properties": {"done": {"type": "boolean"}},
        }
        server.seed([("/output/$schema", schema)])

        rebind = put(path="/output/$schema", value={})
        self.assertFalse(rebind["ok"])
        self.assertIn("read-only", rebind["error"])

        rejected = put(path="/output", value={"done": "not-a-boolean"})
        self.assertFalse(rejected["ok"])
        accepted = put(path="/output", value={"done": True})
        self.assertTrue(accepted["ok"])
        self.assertEqual(get(path="/output")["value"], {"done": True})

    def test_seeded_ref_binding_rejects_agent_supplied_target(self) -> None:
        server, put, get = self._tools()
        server.seed([("/output/$schema", {"$ref": "/schemas/output"})])
        # The forward-declared target is NOT seeded; an agent supplying a
        # permissive schema there must not become the validator.
        planted = put(path="/schemas/output", value={})
        self.assertTrue(planted["ok"])  # plain content write is fine
        result = put(path="/output", value={"anything": 1})
        self.assertFalse(result["ok"])
        self.assertIn("not host-seeded", result["error"])

    def test_event_writer_emits_utc_z_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "pipeline.log"
            try:
                from agents_live.pipeline_mcp import PipelineMcp
            except ImportError:
                from pipeline_mcp import PipelineMcp
            server = PipelineMcp(agent_log=log)
            app = server._build_app()

            result = app._tool_manager.get_tool("put").fn(
                path="/output", value={"done": True})

            self.assertTrue(result["ok"])
            row = json.loads(log.read_text(encoding="utf-8"))
            self.assertEqual(row["log_schema"], 5)
            self.assertTrue(row["ts"].endswith("Z"))
            parsed = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
            self.assertEqual(parsed.utcoffset(), timedelta(0))


class TestStartingThePipelineServer(unittest.TestCase):
    """start() must time the bind, not the import of the mcp SDK (#207)."""

    def _cls(self):
        try:  # installed package layout
            from agents_live.pipeline_mcp import PipelineMcp
        except ImportError:  # flat checkout layout
            from pipeline_mcp import PipelineMcp
        return PipelineMcp

    def test_the_app_is_built_before_the_clock_starts(self) -> None:
        # Importing uvicorn and the mcp SDK costs over a second warm and
        # far more on a cold first run. Doing it inside the server thread
        # spent the caller's timeout on imports, so a slow machine failed
        # a budget meant to cover binding a socket.
        server = self._cls()(require_token=False)
        built_on: list[str] = []
        real_build = server._build_app

        def _record():
            built_on.append(threading.current_thread().name)
            return real_build()

        server._build_app = _record  # type: ignore[method-assign]
        try:
            server.start(timeout=10.0)
        finally:
            server.shutdown()

        self.assertEqual(built_on, [threading.current_thread().name])

    def test_a_build_failure_raises_itself_instead_of_a_timeout(self) -> None:
        # A broken or incompatible mcp SDK used to kill the server thread
        # silently: nothing set the readiness event, so the caller waited
        # out the full timeout and reported what read like a bind
        # failure. The real cause has to reach the caller.
        server = self._cls()(require_token=False)

        def _explode():
            raise ImportError("No module named 'mcp.server.fastmcp'")

        server._build_app = _explode  # type: ignore[method-assign]
        started = time.monotonic()
        with self.assertRaises(ImportError) as caught:
            server.start(timeout=30.0)

        self.assertIn("mcp.server.fastmcp", str(caught.exception))
        self.assertLess(time.monotonic() - started, 30.0)
        self.assertIsNone(server._thread)  # nothing left running

    def test_a_taken_port_is_reported_as_a_failed_bind(self) -> None:
        # The one thing the timeout is now for. The message has to say
        # bind, because that is the only thing left inside the window.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        server = self._cls()(port=blocker.getsockname()[1], require_token=False)

        with self.assertRaises(RuntimeError) as caught:
            server.start(timeout=2.0)

        self.assertIn("failed to bind", str(caught.exception))
        self.assertIn(server.url, str(caught.exception))

    def test_starting_twice_is_refused(self) -> None:
        server = self._cls()(require_token=False)
        server.start(timeout=10.0)
        self.addCleanup(server.shutdown)

        with self.assertRaises(RuntimeError) as caught:
            server.start()

        self.assertIn("called twice", str(caught.exception))


class TestReleaseTool(unittest.TestCase):
    def _load_tool(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "agents_live_release_tool", root / "tools" / "release.py")
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _fixture(self, module, root: Path) -> dict[Path, bytes]:
        module.ROOT = root
        module.PYPROJECT = root / "pyproject.toml"
        module.VERSION_FILES = (
            root / "src" / "agents_live" / "__init__.py",
            root / "src" / "agents_live" / "skill" / "VERSION",
        )
        module.CHANGELOG = (
            root / "src" / "agents_live" / "skill" / "docs" / "changelog.md")
        module.RELEASE_FILES = (
            module.PYPROJECT, *module.VERSION_FILES, module.CHANGELOG)
        contents = (
            'version = "1.2.3"\n',
            '__version__ = "1.2.3"\n',
            "1.2.3\n",
            "# Changelog\n\n## Unreleased\n\n- fix: a fix.\n\n"
            "## 1.2.3 - 2026-07-18\n\n- fix: old release note.\n",
        )
        for path, content in zip(module.RELEASE_FILES, contents):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return {path: path.read_bytes() for path in module.RELEASE_FILES}

    def test_preview_reports_bump_without_modifying_version(self) -> None:
        module = self._load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(module, Path(tmp))
            before = module.PYPROJECT.read_bytes()
            output = io.StringIO()

            with mock.patch("sys.stdout", output):
                module.preview("patch")

            self.assertIn("Release plan: 1.2.3 -> 1.2.4", output.getvalue())
            self.assertIn("Minimum bump from changelog: patch", output.getvalue())
            self.assertIn("git push --atomic", output.getvalue())
            self.assertEqual(module.PYPROJECT.read_bytes(), before)

    def test_preview_rejects_bump_below_changelog_minimum(self) -> None:
        module = self._load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(module, Path(tmp))
            module.CHANGELOG.write_text(
                "# Changelog\n\n## Unreleased\n\n"
                "- feat: add a command.\n\n## 1.2.3\n\nOld.\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(module.ReleaseError, "--bump minor"):
                module.preview("patch")

            output = io.StringIO()
            with mock.patch("sys.stdout", output):
                module.preview("minor")
            self.assertIn("Release plan: 1.2.3 -> 1.3.0", output.getvalue())

    def test_preview_rejects_empty_unreleased_section(self) -> None:
        module = self._load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(module, Path(tmp))
            module.CHANGELOG.write_text(
                "# Changelog\n\n## Unreleased\n\n## 1.2.3\n\nOld.\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(module.ReleaseError, "no release notes"):
                module.preview("patch")

    def test_preview_rejects_incomplete_first_line_summary(self) -> None:
        module = self._load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(module, Path(tmp))
            module.CHANGELOG.write_text(
                "# Changelog\n\n## Unreleased\n\n"
                "- fix: apply the\n"
                "  positional name as an agent filter.\n\n"
                "## 1.2.3\n\n- fix: old release note.\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                module.ReleaseError, "incomplete first-line summary"
            ):
                module.preview("patch")

    def test_release_notes_reject_incomplete_first_line_summary(self) -> None:
        module = self._load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(module, Path(tmp))
            module.CHANGELOG.write_text(
                "# Changelog\n\n## Unreleased\n\n"
                "## 1.2.3 - 2026-07-18\n\n"
                "- fix: reject an incompatible format with a\n"
                "  usage error.\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                module.ReleaseError, "incomplete first-line summary"
            ):
                module._release_notes("1.2.3")

    def test_minimum_bump_detects_breaking_change_markers(self) -> None:
        module = self._load_tool()
        self.assertEqual(module._minimum_bump("- feat!: replace the API."), "major")
        self.assertEqual(
            module._minimum_bump("- fix(parser)!: reject ambiguous input."),
            "major",
        )
        self.assertEqual(
            module._minimum_bump("- feat: replace the API.\n\nBREAKING CHANGE: API v1"),
            "major",
        )
        self.assertEqual(
            module._minimum_bump(
                "- feat: replace the API.\n  BREAKING CHANGE: API v1 is gone."),
            "major",
        )
        # The footer only counts at the start of a line: an entry that
        # describes the handling of a `BREAKING CHANGE:` block is not one.
        self.assertEqual(
            module._minimum_bump(
                "- docs: lift a `BREAKING CHANGE:` block into the notes."),
            "patch",
        )

    def test_version_update_changes_every_release_surface(self) -> None:
        module = self._load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(module, Path(tmp))

            def fake_run(argv, *, capture=False):
                if argv[:2] == ["uv", "version"]:
                    module.PYPROJECT.write_text(
                        'version = "1.2.4"\n', encoding="utf-8")
                return ""

            with mock.patch.object(module, "_run", side_effect=fake_run):
                module._update_versions("1.2.3", "1.2.4")

            for path in module.RELEASE_FILES:
                self.assertIn("1.2.4", path.read_text(encoding="utf-8"))
            changelog = module.CHANGELOG.read_text(encoding="utf-8")
            self.assertIn("## Unreleased\n\n## 1.2.4 - ", changelog)

    def test_prepare_interruption_restores_release_files(self) -> None:
        module = self._load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            original = self._fixture(module, Path(tmp))

            def modify_versions(*_args):
                for path in module.RELEASE_FILES:
                    path.write_text("changed\n", encoding="utf-8")

            with (
                mock.patch.object(module, "_require_tools"),
                mock.patch.object(module, "_print_plan"),
                mock.patch.object(module, "_check_prepare_state"),
                mock.patch.object(module, "_git", return_value="original-head"),
                mock.patch.object(module, "_update_versions",
                                  side_effect=modify_versions),
                mock.patch.object(module, "_check_release_diff",
                                  side_effect=KeyboardInterrupt),
                mock.patch.object(module.subprocess, "run") as run,
                mock.patch("sys.stdout", new_callable=io.StringIO),
                mock.patch("sys.stderr", new_callable=io.StringIO),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    module.prepare("patch")

            for path, content in original.items():
                self.assertEqual(path.read_bytes(), content)
            run.assert_called_once()
            self.assertIn("reset", run.call_args.args[0])

    def test_publish_state_accepts_prepared_and_already_pushed(self) -> None:
        module = self._load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(module, Path(tmp))
            expected = "\n".join(
                path.relative_to(module.ROOT).as_posix()
                for path in module.RELEASE_FILES)

            def git_result(*args):
                values = {
                    ("status", "--porcelain"): "",
                    ("branch", "--show-current"): "main",
                    ("rev-parse", "HEAD"): "release-head",
                    ("rev-parse", "origin/main"): "origin-head",
                    ("rev-list", "--count", "origin/main..HEAD"): "1",
                    ("merge-base", "HEAD", "origin/main"): "origin-head",
                    ("cat-file", "-t", "v1.2.3"): "tag",
                    ("rev-parse", "v1.2.3^{}"): "release-head",
                    ("diff", "--name-only", "HEAD^..HEAD"): expected,
                }
                return values[args]

            with (
                mock.patch.object(module, "_git", side_effect=git_result),
                mock.patch.object(module, "_run"),
            ):
                self.assertTrue(module._check_publish_state("1.2.3"))

            def pushed_git_result(*args):
                if args == ("rev-parse", "origin/main"):
                    return "release-head"
                return git_result(*args)

            with (
                mock.patch.object(module, "_git", side_effect=pushed_git_result),
                mock.patch.object(module, "_run"),
            ):
                self.assertFalse(module._check_publish_state("1.2.3"))

    def test_publish_state_rejects_divergence_and_lightweight_tag(self) -> None:
        module = self._load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(module, Path(tmp))
            base = {
                ("status", "--porcelain"): "",
                ("branch", "--show-current"): "main",
                ("rev-parse", "HEAD"): "release-head",
                ("rev-parse", "origin/main"): "origin-head",
                ("rev-list", "--count", "origin/main..HEAD"): "2",
            }
            with (
                mock.patch.object(module, "_git", side_effect=lambda *args: base[args]),
                mock.patch.object(module, "_run"),
                self.assertRaises(module.ReleaseError),
            ):
                module._check_publish_state("1.2.3")

            base[("rev-list", "--count", "origin/main..HEAD")] = "1"
            base[("merge-base", "HEAD", "origin/main")] = "origin-head"
            base[("cat-file", "-t", "v1.2.3")] = "commit"
            with (
                mock.patch.object(module, "_git", side_effect=lambda *args: base[args]),
                mock.patch.object(module, "_run"),
                self.assertRaises(module.ReleaseError),
            ):
                module._check_publish_state("1.2.3")

    def test_publish_retry_skips_push_and_existing_release_skips_gates(self) -> None:
        module = self._load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(module, Path(tmp))
            existing = subprocess.CompletedProcess(
                args=[], returncode=0, stdout='{"url":"https://example.test/release"}\n')

            with (
                mock.patch.object(module, "_require_tools"),
                mock.patch.object(module, "_check_publish_state", return_value=False),
                mock.patch.object(module.subprocess, "run", return_value=existing),
                mock.patch.object(module, "_run", return_value="") as run,
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                module.publish()
            self.assertEqual(run.call_args_list, [])

            missing = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
            release_bodies = []

            def capture_run(argv, *, capture=False):
                if argv[:3] == ["gh", "release", "create"]:
                    notes_path = Path(argv[argv.index("--notes-file") + 1])
                    release_bodies.append(notes_path.read_text(encoding="utf-8"))
                return ""

            with (
                mock.patch.object(module, "_require_tools"),
                mock.patch.object(module, "_check_publish_state", return_value=False),
                mock.patch.object(module.subprocess, "run", return_value=missing),
                mock.patch.object(module, "_release_notes", return_value="## Changes"),
                mock.patch.object(module, "_run", side_effect=capture_run) as run,
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                module.publish()
            commands = [call.args[0] for call in run.call_args_list]
            self.assertFalse(any(command[:2] == ["git", "push"] for command in commands))
            release_command = next(
                command for command in commands
                if command[:3] == ["gh", "release", "create"]
            )
            self.assertIn("--notes-file", release_command)
            # The body is built here in full; asking gh to generate one is
            # what produced the second, unreconciled list.
            self.assertNotIn("--generate-notes", release_command)
            self.assertEqual(release_bodies, ["## Changes\n"])

    def _notes_fixture(self, module, root: Path) -> None:
        self._fixture(module, root)
        module.CHANGELOG.write_text(
            "# Changelog\n\n## Unreleased\n\n## 1.2.3 - 2026-07-18\n\n"
            "- fix: the last repository can be removed. (#144)\n"
            "  Detail that stays in the changelog.\n"
            "- feat!: own an agent by uuid. (#148)\n"
            "  Detail that stays in the changelog.\n"
            "  BREAKING CHANGE: existing claims do not carry forward, so run\n"
            "  `agents-live start <agent> --transfer-here` on the owning host.\n"
            "- docs: document the seam. (#147)\n",
            encoding="utf-8",
        )

    def test_release_notes_lift_migration_and_annotate_each_row(self) -> None:
        module = self._load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            self._notes_fixture(module, Path(tmp))
            pulls = {
                149: ("fix!: own an agent by uuid", (148,)),
                151: ("fix: let the last repository be removed", (144,)),
                153: ("docs: document the seam", (147,)),
            }

            with (
                mock.patch.object(module, "_previous_tag", return_value="v1.2.2"),
                mock.patch.object(module, "_merged_pulls", return_value=pulls),
            ):
                body = module._release_notes("1.2.3")

            # The migration is lifted out of the changelog rather than left
            # a link away, and reads as a sentence on its own.
            self.assertIn("## Action required\n\nExisting claims do not carry", body)
            # A wrap inside the flag would render it as `--transfer- here`.
            self.assertIn("--transfer-here", body)
            self.assertIn("(PR #149 fixes #148)", body)
            # One list, breaking first, then feat, fix, docs.
            rows = [line for line in body.splitlines() if line.startswith("- ")]
            self.assertEqual(rows, [
                "- feat!: own an agent by uuid (PR #149 fixes #148)",
                "- fix: the last repository can be removed (PR #151 fixes #144)",
                "- docs: document the seam (PR #153 fixes #147)",
            ])
            self.assertNotIn("Curated Summary", body)
            self.assertNotIn("What's Changed", body)
            self.assertIn(
                "[Full changelog](https://github.com/johnshew/agents-live/blob/"
                "v1.2.3/src/agents_live/skill/docs/changelog.md) | "
                "[v1.2.2...v1.2.3](https://github.com/johnshew/agents-live/"
                "compare/v1.2.2...v1.2.3)",
                body,
            )

    def test_release_notes_recover_a_pull_the_changelog_missed(self) -> None:
        module = self._load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            self._notes_fixture(module, Path(tmp))
            pulls = {
                150: ("fix: drop the emoji trailer", (146,)),
                151: ("fix: let the last repository be removed", (144,)),
            }

            with (
                mock.patch.object(module, "_previous_tag", return_value="v1.2.2"),
                mock.patch.object(module, "_merged_pulls", return_value=pulls),
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                body = module._release_notes("1.2.3")

            self.assertIn("- fix: drop the emoji trailer (PR #150 fixes #146)", body)
            self.assertIn("#150 has no changelog entry", stderr.getvalue())

    def test_release_notes_drop_pulls_that_would_duplicate_a_row(self) -> None:
        module = self._load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            self._notes_fixture(module, Path(tmp))
            # Releases organised around an umbrella issue leave sub-pulls
            # closing nothing; joining on the issue alone repeats each row.
            pulls = {
                130: ("docs: document the seam", ()),
                131: ("chore: tidy an unrelated import", ()),
            }

            with (
                mock.patch.object(module, "_previous_tag", return_value="v1.2.2"),
                mock.patch.object(module, "_merged_pulls", return_value=pulls),
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                body = module._release_notes("1.2.3")

            rows = [line for line in body.splitlines() if line.startswith("- ")]
            self.assertEqual(len(rows), 3)
            self.assertNotIn("PR #130", body)
            self.assertNotIn("PR #131", body)
            self.assertIn("#131", stderr.getvalue())

    def test_release_notes_omit_the_action_section_without_a_migration(self) -> None:
        module = self._load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(module, Path(tmp))
            module.CHANGELOG.write_text(
                "# Changelog\n\n## Unreleased\n\n## 1.2.3 - 2026-07-18\n\n"
                "- fix: a fix that needs nothing of the reader.\n",
                encoding="utf-8",
            )

            with (
                mock.patch.object(module, "_previous_tag", return_value=""),
                mock.patch.object(module, "_merged_pulls", return_value={}),
            ):
                body = module._release_notes("1.2.3")

            self.assertNotIn("## Action required", body)
            self.assertIn("- fix: a fix that needs nothing of the reader", body)
            # No prior tag means no comparison link to offer.
            self.assertNotIn("compare/", body)

    def test_reflow_never_breaks_a_hyphenated_token(self) -> None:
        module = self._load_tool()
        # Wrapping on the hyphen renders the flag as `--transfer- here`.
        text = (
            "Claim each one on the machine that should own it by running the "
            "documented command `agents-live start <agent> --transfer-here` "
            "before the next health sweep runs."
        )
        wrapped = module._reflow(text)
        self.assertIn("--transfer-here", wrapped)
        self.assertGreater(len(wrapped.splitlines()), 1)

    def test_annotate_renders_each_reference_kind(self) -> None:
        module = self._load_tool()
        self.assertEqual(module._annotate([151], (144,)), " (PR #151 fixes #144)")
        self.assertEqual(
            module._annotate([143, 145], (137,)), " (PR #143, #145 fixes #137)")
        self.assertEqual(module._annotate([150], ()), " (PR #150)")
        self.assertEqual(module._annotate([], (126,)), " (closes #126)")
        self.assertEqual(module._annotate([], ()), "")


class TestInstallSkill(_TempProject):
    def test_install_then_noop_then_refresh(self) -> None:
        dest = self.root / ".claude" / "skills" / "agents-live"

        self.assertEqual(init.install_skill(self.root), "installed")
        self.assertTrue((dest / "SKILL.md").is_file())

        self.assertIsNone(init.install_skill(self.root))

        version_file = dest / "VERSION"
        if not version_file.is_file():
            # Flat-checkout source payloads carry no VERSION marker (the
            # release assembler stamps it); refresh is version-driven.
            self.skipTest("source payload has no VERSION marker")
        src_version = version_file.read_text(encoding="utf-8")

        # Outdated payload: VERSION differs -> payload replaced,
        # non-payload content (e.g. a scripts/ dir) left alone.
        (dest / "VERSION").write_text("0.0.0\n", encoding="utf-8")
        (dest / "scripts").mkdir()
        (dest / "scripts" / "keep.py").write_text("", encoding="utf-8")
        self.assertEqual(init.install_skill(self.root), "refreshed")
        self.assertEqual(
            (dest / "VERSION").read_text(encoding="utf-8"), src_version)
        self.assertTrue((dest / "scripts" / "keep.py").is_file())

        self.assertIsNone(init.install_skill(self.root))

    def test_upgrade_reports_refresh_then_current(self) -> None:
        with (
            mock.patch.object(
                upgrade, "_targets",
                return_value=([("current project", self.root)], [])),
            mock.patch.object(
                health_check, "ensure_health_cron_lines", return_value=False),
            mock.patch.object(upgrade, "_migrate_triggers") as migrate,
            mock.patch.object(init, "install_skill", return_value="refreshed"),
            mock.patch("builtins.print") as output,
            mock.patch("sys.argv", ["agents-live upgrade", "--skills-only"]),
        ):
            self.assertEqual(upgrade.main(), 0)
        migrate.assert_called_once_with(self.root)
        output.assert_any_call(
            f"{self.root}: upgraded skill payload to match the installed package")
        output.assert_any_call(
            f"Installed agents-live version: {upgrade.__version__}")

        with (
            mock.patch.object(
                upgrade, "_targets",
                return_value=([("current project", self.root)], [])),
            mock.patch.object(
                health_check, "ensure_health_cron_lines", return_value=False),
            mock.patch.object(upgrade, "_migrate_triggers") as migrate,
            mock.patch.object(init, "install_skill", return_value=None),
            mock.patch("builtins.print") as output,
            mock.patch("sys.argv", ["agents-live upgrade", "--skills-only"]),
        ):
            self.assertEqual(upgrade.main(), 0)
        migrate.assert_called_once_with(self.root)
        output.assert_any_call(
            f"{self.root}: skill payload already matches the installed package")

    def test_runtime_upgrade_preserves_receipt_and_converges_plugins(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            mock.patch.object(shutil, "which", return_value="/usr/bin/uv"),
            mock.patch.object(subprocess, "run", return_value=completed) as run,
            mock.patch.object(plugins, "converge", return_value=False) as converge,
            # This test is about the command sent to uv; the process
            # table an upgrade also reads is another test's subject.
            mock.patch.object(hostruntime, "process_command_lines",
                              return_value=[]),
        ):
            self.assertEqual(upgrade._upgrade_runtime(), 0)
        run.assert_called_once_with(
            ["/usr/bin/uv", "tool", "upgrade", "agents-live"],
            check=False,
        )
        converge.assert_called_once_with(
            [], trigger="upgrade", pin_primary=False)

    def test_runtime_install_from_a_local_source_replaces_the_tool(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            mock.patch.object(shutil, "which", return_value="/usr/bin/uv"),
            mock.patch.object(subprocess, "run", return_value=completed) as run,
            mock.patch.object(plugins, "converge", return_value=False),
            mock.patch.object(hostruntime, "process_command_lines",
                              return_value=[]),
        ):
            self.assertEqual(
                upgrade._upgrade_runtime(source=Path("/build/al")), 0)
        run.assert_called_once_with(
            ["/usr/bin/uv", "tool", "install", "--force",
             "--reinstall-package", "agents-live", str(Path("/build/al"))],
            check=False,
        )

    @contextlib.contextmanager
    def _failing_install(self, *, reached_launcher: bool,
                         preexisting: bool = False):
        """A uv install that exits non-zero, having got that far.

        The tool environment is a temporary directory, so the test turns
        on the same evidence as a host - whether uv rewrote the
        environment's launcher before failing - without writing to the
        real one. ``preexisting`` stages the launcher a real host always
        has, backdated so that a rewrite is unambiguous: file timestamps
        come from the coarse system clock, and two writes within one of
        its ticks can otherwise carry the same stamp.
        """
        with tempfile.TemporaryDirectory() as prefix:
            launcher = hostruntime.executable_dir(prefix) / plugins._SHIM_NAME
            launcher.parent.mkdir(parents=True)
            if preexisting:
                launcher.write_bytes(b"old trampoline")
                stale = time.time() - 3600
                os.utime(launcher, (stale, stale))
            failed = subprocess.CompletedProcess(args=[], returncode=2)

            def install(*args, **kwargs):
                if reached_launcher:
                    launcher.write_bytes(b"trampoline")
                return failed

            with (
                mock.patch.object(sys, "prefix", prefix),
                mock.patch.object(subprocess, "run", side_effect=install),
                # These tests are about the launcher; the process table
                # an upgrade also reads is another test's subject, and
                # the stubbed subprocess.run above cannot serve it.
                mock.patch.object(hostruntime, "process_command_lines",
                                  return_value=[]),
            ):
                yield

    def test_a_launcher_left_behind_does_not_fail_an_upgrade_that_happened(
            self) -> None:
        # Windows locks the launcher while any agents-live process runs,
        # including this one, so uv installs the new runtime and then
        # fails to publish the launcher. The runtime is upgraded; the
        # launcher carries no version. Reporting failure would send a
        # person looking for a broken install that is not broken (#179).
        with (
            mock.patch.object(hostruntime, "locks_running_image",
                              return_value=True),
            mock.patch.object(shutil, "which", return_value="/usr/bin/uv"),
            self._failing_install(reached_launcher=True),
            mock.patch.object(plugins, "converge", return_value=False) as converge,
        ):
            self.assertEqual(upgrade._upgrade_runtime(), 0)
        converge.assert_called_once_with(
            [], trigger="upgrade", pin_primary=False)

    def test_an_install_that_never_reached_the_runtime_still_fails(
            self) -> None:
        # The mirror of the case above, and the reason the check is a
        # measurement rather than a reading of uv's message: uv never
        # got as far as the launcher, so it never finished the
        # environment either, so the exit code stands.
        with (
            mock.patch.object(hostruntime, "locks_running_image",
                              return_value=True),
            mock.patch.object(shutil, "which", return_value="/usr/bin/uv"),
            self._failing_install(reached_launcher=False),
            mock.patch.object(plugins, "converge", return_value=False) as converge,
        ):
            self.assertEqual(upgrade._upgrade_runtime(), 2)
        converge.assert_not_called()

    def test_a_stale_launcher_nobody_touched_still_fails_the_install(
            self) -> None:
        # The case a real host is always in: a launcher from the last
        # install is already there. Its presence proves nothing, so the
        # evidence has to be that this install changed it. An install
        # that stopped earlier leaves it alone and keeps its exit code.
        with (
            mock.patch.object(hostruntime, "locks_running_image",
                              return_value=True),
            mock.patch.object(shutil, "which", return_value="/usr/bin/uv"),
            self._failing_install(reached_launcher=False, preexisting=True),
            mock.patch.object(plugins, "converge", return_value=False) as converge,
        ):
            self.assertEqual(upgrade._upgrade_runtime(), 2)
        converge.assert_not_called()

    def test_a_rewritten_launcher_is_recognized_over_a_stale_one(
            self) -> None:
        # The same host, one step further on: uv reached the launcher
        # and rewrote it, so the environment behind it is complete even
        # though the command failed publishing it.
        with (
            mock.patch.object(hostruntime, "locks_running_image",
                              return_value=True),
            mock.patch.object(shutil, "which", return_value="/usr/bin/uv"),
            self._failing_install(reached_launcher=True, preexisting=True),
            mock.patch.object(plugins, "converge", return_value=False) as converge,
        ):
            self.assertEqual(upgrade._upgrade_runtime(), 0)
        converge.assert_called_once_with(
            [], trigger="upgrade", pin_primary=False)

    def test_a_failed_install_off_windows_keeps_its_exit_code(self) -> None:
        # Replacing a running executable is unremarkable on POSIX, so a
        # failure there has some other cause - a directory that cannot
        # be written, a full disk - and excusing it would trade a loud
        # failure for a silent one. Same evidence as the Windows case,
        # opposite conclusion, which is what makes this a seam.
        with (
            mock.patch.object(hostruntime, "locks_running_image",
                              return_value=False),
            mock.patch.object(shutil, "which", return_value="/usr/bin/uv"),
            self._failing_install(reached_launcher=True),
            mock.patch.object(plugins, "converge", return_value=False) as converge,
        ):
            self.assertEqual(upgrade._upgrade_runtime(), 2)
        converge.assert_not_called()

    def test_installing_from_a_path_that_is_not_there_installs_nothing(
            self) -> None:
        # The source is a boundary value: a typo here would otherwise
        # reach uv and be reported in uv's terms.
        missing = self.root / "no-such-build"
        with (
            mock.patch.object(upgrade, "_upgrade_runtime") as runtime,
            mock.patch("sys.argv",
                       ["agents-live upgrade", "--from", str(missing)]),
        ):
            self.assertEqual(upgrade.main(), 1)
        runtime.assert_not_called()

    def test_a_local_install_cannot_be_asked_for_without_installing(
            self) -> None:
        # --skills-only says "install no runtime"; --from says which
        # runtime to install. Accepting both would silently drop one.
        with (
            mock.patch.object(upgrade, "_upgrade_runtime") as runtime,
            mock.patch("sys.argv", ["agents-live upgrade", "--from",
                                    str(self.root), "--skills-only"]),
        ):
            self.assertEqual(upgrade.main(), 1)
        runtime.assert_not_called()

    def test_runtime_upgrade_keeps_coinstalled_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = root / "tool"
            plugin = root / "plugin"
            for package, name in ((tool, "agents-live"), (plugin, "dummy-plugin")):
                module = name.replace("-", "_")
                (package / "src" / module).mkdir(parents=True)
                (package / "src" / module / "__init__.py").write_text(
                    "def main(): pass\n", encoding="utf-8")
                scripts = (
                    '\n[project.scripts]\nagents-live = "agents_live:main"\n'
                    if name == "agents-live" else "")
                (package / "pyproject.toml").write_text(
                    "[build-system]\nrequires = [\"hatchling\"]\n"
                    "build-backend = \"hatchling.build\"\n\n"
                    f"[project]\nname = \"{name}\"\nversion = \"1.0.0\"\n"
                    + scripts,
                    encoding="utf-8",
                )
            wheels = root / "wheels"
            subprocess.run(
                ["uv", "build", "--wheel", "--out-dir", str(wheels), str(plugin)],
                check=True, capture_output=True, text=True)
            plugin_wheel = next(wheels.glob("dummy_plugin-*.whl"))
            environment = {
                "UV_TOOL_DIR": str(root / "tools"),
                "UV_TOOL_BIN_DIR": str(root / "bin"),
            }
            with mock.patch.dict(os.environ, environment):
                subprocess.run(
                    ["uv", "tool", "install", str(tool), "--with", str(plugin_wheel)],
                    check=True, capture_output=True, text=True)
                with mock.patch.object(plugins, "converge", return_value=False):
                    self.assertEqual(upgrade._upgrade_runtime(), 0)
                # A virtualenv puts its interpreter in Scripts on Windows
                # and bin everywhere else.
                venv = root / "tools" / "agents-live"
                tool_python = (venv / "Scripts" / "python.exe"
                               if sys.platform == "win32"
                               else venv / "bin" / "python")
                installed = subprocess.run(
                    [
                        str(tool_python), "-c",
                        "import importlib.metadata; "
                        "print(importlib.metadata.version('dummy-plugin'))",
                    ],
                    check=True, capture_output=True, text=True,
                )
            self.assertEqual(installed.stdout.strip(), "1.0.0")

    def test_plugin_convergence_preserves_receipt_and_unions_declarations(self) -> None:
        first_wheel = self.root / "first.whl"
        second_wheel = self.root / "second.whl"
        first_wheel.write_bytes(b"first")
        second_wheel.write_bytes(b"second")
        first = plugins.Plugin(
            "first-plugin", first_wheel, None, "1.0")
        second = plugins.Plugin(
            "second-plugin", second_wheel, None, "2.0")
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            mock.patch.object(
                plugins, "union",
                return_value={"first-plugin": first, "second-plugin": second}),
            mock.patch.object(
                plugins, "_installed_state",
                side_effect=[(False, "missing"), (True, "installed")]),
            mock.patch.object(plugins, "_integrity_error", return_value=None),
            mock.patch.object(
                plugins, "_receipt_requirements",
                return_value=(
                    plugins.ReceiptRequirement("agents-live==0.3.1"),
                    {"co-installed": plugins.ReceiptRequirement(
                        "/repo/co-installed.whl")},
                )),
            mock.patch.object(plugins, "find_uv", return_value="/usr/bin/uv"),
            mock.patch.object(plugins.subprocess, "run", return_value=completed) as run,
        ):
            self.assertTrue(plugins.converge([self.root]))
        run.assert_called_once_with(
            [
                "/usr/bin/uv", "tool", "install", "--force",
                "agents-live==0.3.1",
                "--with", "/repo/co-installed.whl",
                "--with", str(first_wheel),
                "--with", str(second_wheel),
            ],
            check=False,
        )

    def test_upgrade_discovers_current_and_registered_projects(self) -> None:
        selected = os.environ.pop(paths.ENV_VAR, None)
        try:
            with (
                mock.patch.object(paths, "_walk_for_marker", return_value=self.root),
                mock.patch.object(
                    repos,
                    "entries",
                    return_value=[
                        ("current", str(self.root), None),
                        ("other", "/repos/other", None),
                        ("gone", "/repos/gone", "path is unavailable"),
                    ],
                ),
                mock.patch.object(
                    health_check, "persisted_roots", return_value=[]),
            ):
                targets, errors = upgrade._targets()
        finally:
            if selected is not None:
                os.environ[paths.ENV_VAR] = selected
        self.assertEqual(
            targets,
            [("current project", self.root), ("other", Path("/repos/other"))],
        )
        self.assertEqual(errors, ["gone: path is unavailable"])

    def test_default_upgrade_refreshes_with_newly_installed_cli(self) -> None:
        target = Path("/repos/example")
        with (
            mock.patch.object(upgrade, "_upgrade_runtime", return_value=0) as runtime,
            mock.patch.object(
                upgrade, "_targets", return_value=([("example", target)], [])),
            mock.patch.object(
                upgrade, "_refresh_with_installed_cli", return_value=0) as refresh,
            mock.patch.object(init, "install_skill") as install,
            mock.patch("builtins.print"),
            mock.patch("sys.argv", ["agents-live upgrade"]),
            # AGENTS_LIVE_REPO is set by _TempProject, so main() consults
            # the registry for extra plugin roots; point it at an empty
            # temp registry, not this host's real one (issue #49).
            mock.patch.dict(os.environ, {
                "XDG_CONFIG_HOME": str(self.root / "xdg-config")}),
        ):
            self.assertEqual(upgrade.main(), 0)
        runtime.assert_called_once_with([target], source=None)
        refresh.assert_called_once_with(refresh_skills=True)
        install.assert_not_called()

    def test_runtime_only_upgrade_refreshes_installed_completions(self) -> None:
        with (
            mock.patch.object(upgrade, "_targets", return_value=([], [])),
            mock.patch.object(upgrade, "_upgrade_runtime", return_value=0),
            mock.patch.object(
                upgrade, "_refresh_with_installed_cli", return_value=0) as refresh,
            mock.patch("sys.argv", ["agents-live upgrade", "--runtime-only"]),
        ):
            self.assertEqual(upgrade.main(), 0)
        refresh.assert_called_once_with(refresh_skills=False)

    def test_installed_cli_refreshes_completions_before_skills(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        # str(shim), not the literal: the command carries the path the
        # host spells, and Windows spells this one with backslashes.
        shim = Path("/bin/agents-live")
        with (
            mock.patch.object(
                headless, "cli_shim_path", return_value=shim),
            mock.patch.object(
                subprocess, "run", return_value=completed) as run,
        ):
            self.assertEqual(
                upgrade._refresh_with_installed_cli(refresh_skills=True), 0)
        self.assertEqual(run.call_args_list, [
            mock.call(
                [str(shim), "completions", "--update"], check=False),
            mock.call(
                [str(shim), "upgrade", "--skills-only"], check=False),
        ])

    def test_completion_refresh_failure_does_not_block_skill_refresh(self) -> None:
        failed = subprocess.CompletedProcess(args=[], returncode=1)
        succeeded = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            mock.patch.object(
                headless, "cli_shim_path", return_value=Path("/bin/agents-live")),
            mock.patch.object(
                subprocess, "run", side_effect=[failed, succeeded]) as run,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(
                upgrade._refresh_with_installed_cli(refresh_skills=True), 0)
        self.assertEqual(run.call_count, 2)
        self.assertIn("warning: could not update shell completions",
                      stderr.getvalue())

    def test_completion_launch_failure_does_not_block_skill_refresh(self) -> None:
        succeeded = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            mock.patch.object(
                headless, "cli_shim_path", return_value=Path("/bin/agents-live")),
            mock.patch.object(
                subprocess, "run",
                side_effect=[PermissionError("denied"), succeeded]) as run,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(
                upgrade._refresh_with_installed_cli(refresh_skills=True), 0)
        self.assertEqual(run.call_count, 2)
        self.assertIn("warning: could not update shell completions",
                      stderr.getvalue())

    def test_skills_only_continues_after_project_refresh_failure(self) -> None:
        broken = Path("/repos/broken")
        healthy = Path("/repos/healthy")
        with (
            mock.patch.object(
                upgrade,
                "_targets",
                return_value=([("broken", broken), ("healthy", healthy)], []),
            ),
            mock.patch.object(
                upgrade,
                "_refresh_payload",
                side_effect=[PermissionError("denied"), None],
            ) as refresh,
            mock.patch.object(
                health_check, "ensure_health_cron_lines", return_value=False),
            mock.patch.object(upgrade, "_migrate_triggers"),
            mock.patch("sys.stdout", new_callable=io.StringIO),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            mock.patch("sys.argv", ["agents-live upgrade", "--skills-only"]),
        ):
            self.assertEqual(upgrade.main(), 1)
        self.assertEqual(
            refresh.call_args_list,
            [mock.call(broken), mock.call(healthy)],
        )
        self.assertIn(f"broken ({broken}): denied", stderr.getvalue())

    def test_skills_only_fails_before_refresh_when_trigger_migration_fails(
            self) -> None:
        with (
            mock.patch.object(
                upgrade, "_targets",
                return_value=([("project", self.root)], [])),
            mock.patch.object(
                upgrade, "_migrate_triggers",
                side_effect=OSError("trigger migration failed")),
            mock.patch.object(
                health_check, "ensure_health_cron_lines", return_value=False),
            mock.patch.object(upgrade, "_refresh_payload") as refresh,
            mock.patch("sys.argv", ["agents-live upgrade", "--skills-only"]),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(upgrade.main(), 1)
        refresh.assert_not_called()
        self.assertIn("trigger migration failed", stderr.getvalue())


class TestSpawnInvocation(_TempProject):
    def test_layout_appropriate_argv(self) -> None:
        scripts = self.root / ".claude" / "skills" / "agents-live" / "scripts"
        scripts.mkdir(parents=True)
        run_script = scripts / "run.py"
        run_script.write_text("", encoding="utf-8")
        argv = spawn._run_invocation(self.root, "demo")
        if headless.packaged_execution():
            # Shim form when resolvable; None (logged skip) when the
            # shim is absent from the test environment.
            if argv is not None:
                self.assertEqual(
                    argv[1:],
                    ["--repo", str(self.root), "run", "--name", "demo"])
        else:
            self.assertIsNotNone(argv)
            self.assertIn(str(run_script), argv)
            self.assertEqual(argv[-2:], ["--name", "demo"])

    def test_flat_layout_without_run_script_skips(self) -> None:
        if headless.packaged_execution():
            self.skipTest("packaged layout resolves via the shim")
        self.assertIsNone(spawn._run_invocation(self.root, "demo"))


class TestJudgingASpawnedChild(_TempProject):
    """The liveness check reads the exit status, not the clock."""

    def _dispatch(self, exit_code: int | None):
        proc = mock.Mock(pid=4321)
        proc.poll.return_value = exit_code
        runtime = mock.Mock()
        runtime.spawn_detached.return_value = proc
        with (
            mock.patch.object(spawn, "_run_invocation",
                              return_value=["uv", "run", "run.py"]),
            mock.patch.object(spawn, "_hostruntime", return_value=runtime),
            mock.patch.object(spawn.time, "sleep"),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            result = spawn.spawn_agent(self.root, "demo", ["a.md"])
        return proc, result, stderr.getvalue()

    def test_a_child_still_running_is_a_dispatch(self) -> None:
        proc, result, stderr = self._dispatch(None)
        self.assertIs(result, proc)
        self.assertNotIn("WARNING", stderr)

    def test_a_child_that_finished_cleanly_is_a_dispatch(self) -> None:
        # The paths that finish inside the sample window finish on
        # purpose: a pre-processor skip, an agent this host does not own.
        # Reporting them as deaths made success a race against the host.
        proc, result, stderr = self._dispatch(0)
        self.assertIs(result, proc)
        self.assertNotIn("WARNING", stderr)

    def test_a_child_that_failed_immediately_is_reported(self) -> None:
        _, result, stderr = self._dispatch(3)
        self.assertIsNone(result)
        self.assertIn("exited 3 immediately", stderr)


class TestStateHome(_TempProject):
    def test_watcher_dispatch_logs_state_home_captures_without_crashing(self) -> None:
        # Run captures live outside the repository now; rendering them
        # repo-relative raised ValueError and killed the watcher process
        # on its first dispatch (2026-07-19).
        completed = mock.Mock(pid=4242)
        completed.wait.return_value = 0
        events: list[dict] = []
        with (
            mock.patch.object(activate.subprocess, "Popen",
                              return_value=completed),
            mock.patch.object(activate, "log_event",
                              lambda _log, **fields: events.append(fields)),
            mock.patch.object(activate, "run_invocation",
                              return_value=["true"]),
        ):
            activate._dispatch_run_once("demo", ["some/file.md"])
        start = next(e for e in events if e.get("status") == "start")
        self.assertTrue(Path(start["stdout"]).is_absolute())
        self.assertIn(str(paths.repo_state_dir(self.root)), start["stdout"])

    def test_path_backed_watcher_artifacts_stay_in_state_home(self) -> None:
        prompt = self.root / "my-agent.md"
        prompt.write_text(AGENT_DEFINITION, encoding="utf-8")
        hash_path = activate._watch_hash_path(str(prompt))
        self.assertEqual(hash_path.parent, paths.repo_state_dir(self.root))
        self.assertNotIn(str(prompt.parent), hash_path.name)

        completed = mock.Mock(pid=4242)
        completed.wait.return_value = 0
        events: list[dict] = []
        with (
            mock.patch.object(activate.subprocess, "Popen",
                              return_value=completed),
            mock.patch.object(activate, "log_event",
                              lambda _log, **fields: events.append(fields)),
            mock.patch.object(activate, "run_invocation",
                              return_value=["true"]),
        ):
            activate._dispatch_run_once(str(prompt), ["some/file.md"])
        start = next(event for event in events if event.get("status") == "start")
        self.assertTrue(Path(start["stdout"]).is_relative_to(
            paths.repo_state_dir(self.root)))
        self.assertFalse((prompt.parent / "runs").exists())

    def test_state_home_honors_xdg_env(self) -> None:
        self.assertEqual(
            paths.state_home(), self.root / "xdg-state" / "agents-live")
        self.assertEqual(paths.host_logs_dir(), paths.state_home() / "logs")
        self.assertEqual(
            paths.health_beacon_path(), paths.state_home() / "health.ok")

    def test_repo_state_key_is_stable_and_distinct(self) -> None:
        key = paths.repo_state_key(self.root)
        self.assertEqual(key, paths.repo_state_key(self.root))
        self.assertTrue(key.startswith(f"{self.root.name}-"))
        other = self.root / "Agents"
        self.assertNotEqual(key, paths.repo_state_key(other))

    def test_logs_root_lives_under_state_home_not_the_tree(self) -> None:
        root = headless.logs_root()
        self.assertEqual(root, paths.repo_state_dir(self.root) / "logs")
        self.assertNotIn(str(self.root / "Agents"), str(root))

_HEALTH_SHIM = Path("/opt/agents-live/bin/agents-live")


class TestHealthCheckLoop(_TempProject):
    def _canonical_lines(self) -> list[str]:
        with mock.patch.object(
                health_check, "cli_shim_path", return_value=_HEALTH_SHIM):
            return health_check.build_health_cron_lines()

    def test_cron_lines_are_host_scoped(self) -> None:
        lines = self._canonical_lines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("@reboot "))
        self.assertTrue(lines[1].startswith("0 * * * * "))
        for line in lines:
            # Host-level: no `cd` into a project and no pinned --repo.
            self.assertNotIn(" cd ", f" {line}")
            self.assertNotIn("--repo", line)
            self.assertIn("internal maintain --quiet", line)
            self.assertTrue(health_check.health_cron_line_matches(line))

    def test_matcher_ignores_legacy_agent_and_foreign_lines(self) -> None:
        legacy = ("0 * * * * cd /some/project && PATH=/usr/bin "
                  "/usr/local/bin/agents-live --repo /some/project run "
                  "--name agents-live-health-check --quiet 2>&1")
        self.assertFalse(health_check.health_cron_line_matches(legacy))
        self.assertFalse(health_check.health_cron_line_matches(
            "0 * * * * /usr/bin/backup health-check 2>&1"))

    def test_path_backed_watcher_is_loaded_from_persisted_intent(self) -> None:
        prompt = self.root / "my-agent.md"
        prompt.write_text(AGENT_DEFINITION, encoding="utf-8")
        states: dict[str, dict] = {}
        with (
            # An empty table, not the host's own: reading the developer's
            # crontab makes the result depend on their machine, and on a
            # host without the command at all it is an outright crash.
            mock.patch.object(
                headless, "current_crontab_lines", return_value=[]),
            mock.patch.object(
                schedules, "watcher_respawn_names",
                return_value=[str(prompt)]),
        ):
            health_check._add_persisted_agent_states(states)
        self.assertIn(str(prompt), states)
        self.assertEqual(states[str(prompt)]["name"], "my-agent")

    def test_repair_dry_run_is_empty_when_schedule_is_converged(self) -> None:
        canonical = self._canonical_lines()
        with (
            mock.patch.object(health_check, "cli_shim_path",
                              return_value=_HEALTH_SHIM),
            mock.patch.object(headless, "current_crontab_lines",
                              return_value=canonical),
            mock.patch.object(health_check, "_registered_roots",
                              return_value=[]),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(health_check.repair(dry_run=True), 0)
        self.assertEqual(json.loads(stdout.getvalue())["actions"], [])

    def test_repair_dry_run_reports_schedule_replacement(self) -> None:
        with (
            mock.patch.object(health_check, "cli_shim_path",
                              return_value=_HEALTH_SHIM),
            mock.patch.object(headless, "current_crontab_lines",
                              return_value=[]),
            mock.patch.object(health_check, "_registered_roots",
                              return_value=[]),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(health_check.repair(dry_run=True), 0)
        actions = json.loads(stdout.getvalue())["actions"]
        self.assertEqual(actions[0]["action"], "replace-maintenance-schedule")
        self.assertEqual(actions[0]["add"], self._canonical_lines())

    def test_repair_dry_run_reports_plugin_and_workspace_mutations(self) -> None:
        plugin = plugins.Plugin(
            "example-plugin", self.root / "example.whl", None, "1.0")
        migration = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({
                "plan": {
                    "schedule": {
                        "scheduled": [["old schedule"], ["new schedule"]]},
                    "watcher": {},
                    "missing": ["deleted"],
                },
            }), stderr="")
        workspace = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({
                "actions": [
                    {"action": "restart-watcher", "agent": "watched"},
                    {"action": "deactivate-for-ownership", "agent": "remote"},
                ],
            }), stderr="")
        with (
            mock.patch.object(health_check, "cli_shim_path",
                              return_value=_HEALTH_SHIM),
            mock.patch.object(headless, "current_crontab_lines",
                              return_value=self._canonical_lines()),
            mock.patch.object(health_check, "_registered_roots",
                              return_value=[("project", self.root)]),
            mock.patch.object(health_check.plugins, "union",
                              return_value={"example-plugin": plugin}),
            mock.patch.object(health_check.plugins, "_installed_state",
                              return_value=(False, "missing")),
            mock.patch.object(health_check.subprocess, "run",
                              side_effect=[migration, workspace]),
            mock.patch.object(health_check, "_resolve_smoketest_runtime",
                              return_value=None),
            mock.patch.object(health_check, "_git_head", return_value=None),
            mock.patch.object(health_check.repos, "default_root",
                              return_value=self.root),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(health_check.repair(dry_run=True), 0)
        actions = json.loads(stdout.getvalue())["actions"]
        self.assertEqual(
            [action["action"] for action in actions],
            [
                "converge-plugins",
                "rewrite-schedule",
                "prune-orphaned-trigger",
                "restart-watcher",
                "deactivate-for-ownership",
            ],
        )

    def test_workspace_repair_plan_ignores_unavailable_git(self) -> None:
        with (
            mock.patch.object(health_check, "_agent_states", return_value={}),
            mock.patch.object(health_check, "_add_persisted_agent_states"),
            mock.patch("agents_live.activate.list_active_agent_names",
                       return_value=set()),
            mock.patch.object(health_check, "list_agents", return_value=[]),
            mock.patch.object(health_check.ownership, "load_owners",
                              return_value={}),
            mock.patch.object(
                schedules, "watcher_respawn_names",
                return_value=[]),
            mock.patch.object(health_check, "_git_head", return_value="abc"),
            mock.patch.object(health_check.subprocess, "run",
                              side_effect=FileNotFoundError("git missing")),
        ):
            self.assertEqual(health_check.plan_sweep(), [])

    def test_workspace_repair_plan_uses_remote_main_for_registry_prune(
            self) -> None:
        def plan(remote_sha: str) -> list[dict]:
            remote = subprocess.CompletedProcess(
                [], 0, stdout=f"{remote_sha}\trefs/heads/main\n", stderr="")
            with (
                mock.patch.object(health_check, "_agent_states", return_value={}),
                mock.patch.object(health_check, "_add_persisted_agent_states"),
                mock.patch("agents_live.activate.list_active_agent_names",
                           return_value=set()),
                mock.patch.object(health_check, "list_agents", return_value=[]),
                mock.patch.object(health_check.ownership, "load_owners",
                                  return_value={"deleted": "this-host"}),
                mock.patch.object(health_check.ownership, "current_host",
                                  return_value="this-host"),
                mock.patch.object(
                    schedules, "watcher_respawn_names",
                    return_value=[]),
                mock.patch.object(health_check, "_git_head", return_value="head"),
                mock.patch.object(health_check.subprocess, "run",
                                  return_value=remote),
                mock.patch.object(health_check, "_agent_definition_exists",
                                  return_value=False),
            ):
                return health_check.plan_sweep()

        self.assertEqual(plan("newer-remote"), [])
        self.assertEqual(plan("head"), [
            {"action": "prune-ownership-record", "agent": "deleted"},
        ])

    def test_internal_dry_run_requires_workspace_sweep(self) -> None:
        with (
            mock.patch("sys.argv", ["agents-live internal maintain", "--dry-run"]),
            mock.patch.object(health_check, "run_host_loop") as host_loop,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            with self.assertRaises(SystemExit) as raised:
                health_check.main()
        self.assertEqual(raised.exception.code, 2)
        host_loop.assert_not_called()
        self.assertIn("--dry-run requires --sweep", stderr.getvalue())

    def test_repair_dry_run_skips_smoketest_without_git_head(self) -> None:
        migration = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({
                "plan": {"schedule": {}, "watcher": {}, "missing": []},
            }), stderr="")
        workspace = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"actions": []}), stderr="")
        with (
            mock.patch.object(health_check, "cli_shim_path",
                              return_value=_HEALTH_SHIM),
            mock.patch.object(headless, "current_crontab_lines",
                              return_value=self._canonical_lines()),
            mock.patch.object(health_check, "_registered_roots",
                              return_value=[("global", self.root)]),
            mock.patch.object(health_check.plugins, "union", return_value={}),
            mock.patch.object(health_check.subprocess, "run",
                              side_effect=[migration, workspace]),
            mock.patch.object(health_check, "_resolve_smoketest_runtime",
                              return_value="agency"),
            mock.patch.object(health_check, "_git_head", return_value=None),
            mock.patch.object(health_check.repos, "default_root",
                              return_value=None),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(health_check.repair(dry_run=True), 0)
        self.assertEqual(json.loads(stdout.getvalue())["actions"], [])

    def test_repair_dry_run_runs_smoketest_when_prior_sha_is_missing(self) -> None:
        migration = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({
                "plan": {"schedule": {}, "watcher": {}, "missing": []},
            }), stderr="")
        workspace = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"actions": []}), stderr="")
        with (
            mock.patch.object(health_check, "cli_shim_path",
                              return_value=_HEALTH_SHIM),
            mock.patch.object(headless, "current_crontab_lines",
                              return_value=self._canonical_lines()),
            mock.patch.object(health_check, "_registered_roots",
                              return_value=[("project", self.root)]),
            mock.patch.object(health_check.plugins, "union", return_value={}),
            mock.patch.object(health_check.subprocess, "run",
                              side_effect=[migration, workspace]),
            mock.patch.object(health_check, "_resolve_smoketest_runtime",
                              return_value="agency"),
            mock.patch.object(health_check, "_git_head", return_value="abc"),
            mock.patch.object(health_check, "smoketest_source_fingerprint",
                              return_value="same"),
            mock.patch.object(health_check, "_load_previous_beacon",
                              return_value={"smoketest": {
                                  "status": "pass",
                                  "source_fingerprint": "same",
                              }}),
            mock.patch.object(health_check.repos, "default_root",
                              return_value=self.root),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(health_check.repair(dry_run=True), 0)
        self.assertEqual(
            json.loads(stdout.getvalue())["actions"][0]["action"],
            "run-smoketest",
        )

    def test_repair_dry_run_deduplicates_overlapping_orphans(self) -> None:
        migration = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({
                "plan": {
                    "schedule": {}, "watcher": {}, "missing": ["deleted"]},
            }), stderr="")
        workspace = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({
                "actions": [
                    {"action": "prune-orphaned-trigger", "agent": "deleted"}],
            }), stderr="")
        with (
            mock.patch.object(health_check, "cli_shim_path",
                              return_value=_HEALTH_SHIM),
            mock.patch.object(headless, "current_crontab_lines",
                              return_value=self._canonical_lines()),
            mock.patch.object(health_check, "_registered_roots",
                              return_value=[("project", self.root)]),
            mock.patch.object(health_check.plugins, "union", return_value={}),
            mock.patch.object(health_check.subprocess, "run",
                              side_effect=[migration, workspace]),
            mock.patch.object(health_check, "_resolve_smoketest_runtime",
                              return_value=None),
            mock.patch.object(health_check, "_git_head", return_value=None),
            mock.patch.object(health_check.repos, "default_root",
                              return_value=self.root),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(health_check.repair(dry_run=True), 0)
        actions = json.loads(stdout.getvalue())["actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "prune-orphaned-trigger")
        self.assertTrue(all(
            action.get("workspace") == str(self.root)
            for action in actions[1:]
        ))

    def test_workspace_repair_plan_does_not_mutate_lifecycle(self) -> None:
        states = {
            "remote": {
                "state": "active", "triggerStates": {"watcher": "active"}},
            "watched": {
                "state": "stopped", "triggerStates": {"watcher": "stopped"}},
        }
        git_result = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with (
            mock.patch.object(health_check, "_agent_states",
                              return_value=states),
            mock.patch.object(health_check, "_add_persisted_agent_states"),
            mock.patch("agents_live.activate.list_active_agent_names",
                       return_value={"orphan"}),
            mock.patch.object(health_check, "list_agents", return_value=[]),
            mock.patch.object(health_check, "agent_file_exists",
                              return_value=False),
            mock.patch.object(health_check.ownership, "load_owners",
                              return_value={"remote": "other-host"}),
            mock.patch.object(health_check.ownership, "current_host",
                              return_value="this-host"),
            mock.patch.object(
                schedules, "watcher_respawn_names",
                return_value=["remote", "watched"]),
            mock.patch.object(health_check, "_git_head", return_value=None),
            mock.patch.object(health_check.subprocess, "run",
                              return_value=git_result),
            mock.patch.object(health_check, "_lifecycle") as lifecycle,
        ):
            actions = health_check.plan_sweep()
        lifecycle.assert_not_called()
        self.assertEqual(
            [action["action"] for action in actions],
            [
                "prune-orphaned-trigger",
                "deactivate-for-ownership",
                "restart-watcher",
            ],
        )

    def test_ensure_converges_and_respects_opt_in(self) -> None:
        installed: dict[str, list[str]] = {}
        foreign = "0 1 * * * /usr/bin/foreign-job 2>&1"
        stale = "@reboot PATH=/old /old/bin/agents-live health-check --quiet 2>&1"

        def fake_install(lines: list[str]) -> None:
            installed["lines"] = list(lines)

        with (
            mock.patch.object(health_check, "cli_shim_path",
                              return_value=_HEALTH_SHIM),
            mock.patch.object(headless, "current_crontab_lines",
                              return_value=[foreign]),
            mock.patch.object(headless, "install_crontab", fake_install),
        ):
            # Not installed + install=False: never adds maintenance.
            self.assertFalse(
                health_check.ensure_health_cron_lines(install=False))
            self.assertNotIn("lines", installed)
            # Opt-in installs both entries and keeps foreign lines.
            self.assertTrue(health_check.ensure_health_cron_lines())
            self.assertEqual(
                installed["lines"], [foreign] + self._canonical_lines())

        with (
            mock.patch.object(health_check, "cli_shim_path",
                              return_value=_HEALTH_SHIM),
            mock.patch.object(headless, "current_crontab_lines",
                              return_value=[stale, foreign]),
            mock.patch.object(headless, "install_crontab", fake_install),
        ):
            # Present but stale: converged even with install=False (an
            # upgrade re-homes the pinned shim path).
            self.assertTrue(
                health_check.ensure_health_cron_lines(install=False))
            self.assertEqual(
                installed["lines"], [foreign] + self._canonical_lines())

    def test_remove_deletes_only_health_lines(self) -> None:
        installed: dict[str, list[str]] = {}
        foreign = "0 1 * * * /usr/bin/foreign-job 2>&1"
        with (
            mock.patch.object(headless, "current_crontab_lines",
                              return_value=[foreign] + self._canonical_lines()),
            mock.patch.object(headless, "install_crontab",
                              lambda lines: installed.update(lines=list(lines))),
        ):
            self.assertTrue(health_check.remove_health_cron_lines())
            self.assertEqual(installed["lines"], [foreign])
        with mock.patch.object(headless, "current_crontab_lines",
                               return_value=[foreign]):
            self.assertFalse(health_check.remove_health_cron_lines())

    def test_sweep_reports_degraded_ownership_without_aborting(self) -> None:
        # The 2026-07-19 incident class: ownership unavailable must
        # degrade the sweep, never kill the loop.
        with (
            mock.patch.object(
                headless, "current_crontab_lines", return_value=[]),
            mock.patch.object(
                health_check.ownership, "load_owners",
                side_effect=health_check.ownership.OwnershipUnavailableError(
                    "no backend")),
            mock.patch.object(health_check, "_converge_triggers",
                              return_value=True),
            mock.patch.object(health_check, "_origin_main_synced",
                              return_value=False),
        ):
            result = health_check.sweep()
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["ownership_degraded"])
        self.assertTrue(result["registry_prune_abstained"])

    def test_sweep_stdout_stays_pure_json_when_pruning_prints(self) -> None:
        # activate.prune_orphans reports each pruned entry on stdout; the
        # sweep's stdout contract is exactly one JSON document (the host
        # loop parses it), so in-process prints must be diverted.
        def noisy_prune() -> list[str]:
            print("Removed cron entries for 'legacy-agent'")
            return ["legacy-agent"]

        with (
            mock.patch.object(
                headless, "current_crontab_lines", return_value=[]),
            mock.patch.object(activate, "prune_orphans", noisy_prune),
            mock.patch.object(health_check, "_converge_triggers",
                              return_value=True),
            mock.patch.object(health_check, "_origin_main_synced",
                              return_value=False),
            mock.patch.object(
                health_check.ownership, "load_owners",
                side_effect=health_check.ownership.OwnershipUnavailableError(
                    "no backend")),
            mock.patch.object(sys, "argv",
                              ["agents-live internal maintain", "--sweep"]),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (contextlib.redirect_stdout(stdout),
                  contextlib.redirect_stderr(stderr)):
                code = health_check.main()
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())  # exactly one JSON document
        self.assertEqual(payload["status"], "ok")
        self.assertIn("legacy-agent", stderr.getvalue())

    def test_maintenance_is_internal_only(self) -> None:
        self.assertNotIn("health-check", cli.COMMAND_BY_NAME)
        internal = cli.COMMAND_BY_NAME["internal"]
        command = next(
            child for child in internal.subcommands if child.name == "maintain")
        self.assertEqual(command.module, "health_check")
        self.assertTrue(command.hidden)


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------
#
# Assertions that relate two facts in the tree which have to agree, where
# the failure mode is drift rather than logic (#184). They patch nothing,
# so there is no belief about a seam to encode and be wrong about
# together with the code. Each one is here because the corresponding
# disagreement shipped at least once through a green suite.


def _package_modules() -> list[Path]:
    """Every module of the package, in either layout."""
    return sorted(Path(hostruntime.__file__).resolve().parent.glob("*.py"))


def _code_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string literal in *tree* except the docstrings.

    Comments never reach the AST and docstrings are excluded here, so
    what is left is what the module says to the operating system rather
    than what it says to a reader.
    """
    documentation = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in documentation]


class TestPlatformSeam(unittest.TestCase):
    """The platform seam described in docs/windows-support.md.

    The invariant that document states was true when it was written and
    had quietly stopped being true in five modules by the time anyone
    looked (#191). Stating it in prose asks every future change to
    remember it; asserting it here makes the suite remember instead.
    """

    #: Modules allowed to name a Windows program or bind a Windows API.
    #: The first five implement the platform; `schedules` and
    #: `watchsource` choose between the two mechanisms and name both.
    SEAM = {"hostruntime.py", "wintasks.py", "winwatch.py", "hidden.py",
            "heartbeat.py", "schedules.py", "watchsource.py"}

    #: Programs and entry points that exist on one platform only. A
    #: module that names one of these is doing platform-specific work,
    #: whatever it calls the variable it stores the answer in. What a
    #: module prints about a platform is not covered: a check label or a
    #: remedy naming Windows is reporting, not dispatching.
    PLATFORM_TOKENS = ("win32", "schtasks", "windll", "powershell.exe",
                       "pythonw", "wscript.exe", "wsl.exe", "wslg.exe",
                       "readdirectorychangesw")

    def modules_outside_the_seam(self) -> list[Path]:
        return [path for path in _package_modules()
                if path.name not in self.SEAM]

    def test_the_seam_is_the_modules_it_names(self) -> None:
        # A renamed seam module would leave a name here that exempts
        # nothing, and the scans below would quietly stop covering its
        # replacement while still passing.
        present = {path.name for path in _package_modules()}
        self.assertLessEqual(self.SEAM, present)
        self.assertGreater(len(self.modules_outside_the_seam()),
                           len(self.SEAM))

    def test_only_the_seam_names_a_platform(self) -> None:
        for path in self.modules_outside_the_seam():
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for lineno, value in _code_strings(tree):
                    for token in self.PLATFORM_TOKENS:
                        self.assertNotIn(
                            token, value.lower(),
                            f"{path.name}:{lineno} names {token!r}; ask the "
                            "host runtime, or move the work into the seam")

    def test_only_the_seam_binds_a_windows_api(self) -> None:
        # ctypes reaches a Windows entry point by attribute, so the name
        # never appears as a string.
        for path in self.modules_outside_the_seam():
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Attribute):
                        continue
                    self.assertNotIn(
                        node.attr.lower(), ("windll", "readdirectorychangesw"),
                        f"{path.name}:{node.lineno} binds {node.attr}")

    def test_the_task_folder_is_named_once(self) -> None:
        # Two spellings of the folder is how a preflight came to query a
        # folder the registration never wrote to (#191).
        named = []
        for path in _package_modules():
            for lineno, value in _code_strings(
                    ast.parse(path.read_text(encoding="utf-8"))):
                if wintasks.TASK_FOLDER.lower() in value.lower():
                    named.append(f"{path.name}:{lineno}")
        self.assertEqual(named, [f"wintasks.py:{_task_folder_lineno()}"])


def _task_folder_lineno() -> int:
    source = Path(wintasks.__file__).resolve()
    for number, line in enumerate(
            source.read_text(encoding="utf-8").splitlines(), start=1):
        if line.startswith("TASK_FOLDER"):
            return number
    raise AssertionError("wintasks no longer defines TASK_FOLDER")


class TestAgreementsAcrossModules(unittest.TestCase):
    """Facts held in two places that have to keep saying the same thing."""

    def test_the_smoketest_waits_longer_than_an_agent_may_take(self) -> None:
        # The framework retries a timed-out agent, so the worst case is
        # the per-attempt timeout times the attempts. A wait shorter than
        # that fails a smoketest step over an agent that was still
        # working (#178), and both budgets were plain literals when it
        # last happened.
        self.assertGreaterEqual(
            smoketest.AGENT_RESULT_TIMEOUT_S,
            headless.HEADLESS_TIMEOUT * (headless.HEADLESS_TIMEOUT_RETRIES + 1))

    def test_the_busy_exit_status_means_the_same_on_both_sides(self) -> None:
        # smoketest returns this to say "another run holds the lock" and
        # health_check reads it to tell that apart from a real failure.
        # Each module spells the number itself, so a change to one side
        # turns a declined run into a reported failure with nothing
        # failing.
        self.assertEqual(smoketest.SMOKETEST_BUSY_EXIT,
                         health_check.SMOKETEST_BUSY_EXIT)

    def test_smoketest_fixtures_are_exempt_from_ownership(self) -> None:
        # run.py exempts _-prefixed agents from the ownership gate so
        # the smoketest passes whatever the registry says. A fixture
        # renamed without the prefix would be skipped as foreign on any
        # host that does not own it, and the step would fail for a
        # reason nothing in it mentions.
        for name in smoketest.SMOKETEST_AGENT_NAMES:
            self.assertTrue(name.startswith("_"), name)

    def test_every_capability_a_command_declares_can_be_probed(self) -> None:
        # A probe named in the spec but missing here raises KeyError
        # inside the preflight, on that command only, on the host that
        # runs it.
        declared = set()
        for command in COMMANDS:
            declared.update(command.probes)
            for child in command.subcommands:
                declared.update(child.probes)
        self.assertLessEqual(declared, set(preflight._CAPABILITY_PROBES))

    def test_doctor_describes_every_mechanism_a_host_may_have(self) -> None:
        # Doctor selects its row by the mechanism the runtime reports, so
        # a mechanism added on one side and not the other is a KeyError
        # on the host that has it.
        self.assertEqual(
            set(doctor._MECHANISMS["schedule"]),
            {hostruntime.CRONTAB, hostruntime.TASK_SCHEDULER})
        self.assertEqual(
            set(doctor._MECHANISMS["watch"]),
            {watchsource.INOTIFY, watchsource.DIRECTORY_CHANGES})
        self.assertIn(hostruntime.native_scheduler(),
                      doctor._MECHANISMS["schedule"])
        self.assertIn(watchsource.mechanism(), doctor._MECHANISMS["watch"])

    def test_the_release_gate_smoketests_this_checkout(self) -> None:
        # Every release to date gated on whatever project root happened
        # to resolve on the releasing host, through seventeen passing
        # tests that patched the call instead of reading it (#184).
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "agents_live_release_gate", root / "tools" / "release.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        command = module._smoketest_command()
        self.assertIn("--repo", command)
        self.assertEqual(command[command.index("--repo") + 1], str(module.ROOT))
        # The plan a developer reads before saying yes has to be the
        # commands that then run.
        self.assertIn(command, module._gate_commands())

    def test_the_publish_workflow_runs_the_declared_gates(self) -> None:
        # The gates were spelled out again in YAML, one of them lost a
        # dependency the local run kept, and the release failed after the
        # tag and the GitHub release existed (#218). The workflow now
        # runs the list release.py declares, and everything in that list
        # but the smoketest, which needs a live agent CLI.
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "publish.yml").read_text(
            encoding="utf-8")
        self.assertIn("tools/release.py --gates", workflow)
        restated = [line.strip() for line in workflow.splitlines()
                    if "run:" in line
                    and any(gate in line for gate in
                            ("pre-release-audit", "test_smoke", "uv build"))]
        self.assertEqual(restated, [])
        spec = importlib.util.spec_from_file_location(
            "agents_live_release_gates", root / "tools" / "release.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ran: list[list[str]] = []
        with mock.patch.object(module, "_run", lambda argv, **kw: ran.append(argv)), \
                contextlib.redirect_stdout(io.StringIO()):
            module.gates()
        self.assertEqual(ran, [command for command in module._gate_commands()
                               if command != module._smoketest_command()])


def _catches_value_error(handler: ast.ExceptHandler) -> bool:
    """Whether *handler* would catch a failed root resolution."""
    if handler.type is None:
        return True
    named = (handler.type.elts if isinstance(handler.type, ast.Tuple)
             else [handler.type])
    return any(getattr(node, "id", getattr(node, "attr", "")) in
               ("ValueError", "Exception", "BaseException") for node in named)


def _unguarded_root_calls(tree: ast.AST, names: tuple[str, ...]) -> list[int]:
    """Lines where importing the module would resolve a project root.

    Function bodies are skipped: they run when they are called, which is
    the point. A call inside a try that handles the failure is not an
    import-time hazard either - the module has already decided what to
    do without a root.
    """
    found: list[int] = []

    def walk(node: ast.AST, guarded: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            child_guarded = guarded or (
                isinstance(child, ast.Try)
                and any(_catches_value_error(handler)
                        for handler in child.handlers))
            if (isinstance(child, ast.Call) and not child_guarded
                    and getattr(child.func, "id",
                                getattr(child.func, "attr", "")) in names):
                found.append(child.lineno)
            walk(child, child_guarded)

    walk(tree, False)
    return found


class TestImportingAModuleNeedsNoProject(unittest.TestCase):
    """No module may resolve a project root while it is being imported.

    Resolving at import turns a missing root into a traceback from the
    import statement, before the command that would explain it exists
    (#202). The tree held three answers to this one question: two
    modules resolved outright, a third guarded, and the fix for the same
    defect in the smoketest (#184) had already established which answer
    is right.
    """

    #: Resolvers that cannot answer without a project.
    ROOT_CALLS = ("resolve_root", "repo_root")

    def test_no_module_resolves_a_root_while_it_loads(self) -> None:
        for path in _package_modules():
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    _unguarded_root_calls(tree, self.ROOT_CALLS), [],
                    f"{path.name} resolves a project root at import; move it "
                    "into the function that needs the path, or handle the "
                    "failure here")

    def test_the_check_can_tell_the_two_apart(self) -> None:
        # An invariant that cannot fail is worse than none: it reads as
        # coverage while asserting nothing.
        self.assertEqual(
            _unguarded_root_calls(
                ast.parse("REPO = resolve_root()"), self.ROOT_CALLS), [1])
        self.assertEqual(
            _unguarded_root_calls(ast.parse(
                "try:\n REPO = resolve_root()\nexcept ValueError:\n REPO = None"
            ), self.ROOT_CALLS), [])
        self.assertEqual(
            _unguarded_root_calls(
                ast.parse("def f():\n return resolve_root()"),
                self.ROOT_CALLS), [])


class TestReadingLogsOutsideAProject(_TempProject):
    """What the log readers do when no project resolves."""

    @contextlib.contextmanager
    def _no_project(self):
        os.environ.pop(paths.ENV_VAR, None)
        saved = Path.cwd()
        with tempfile.TemporaryDirectory() as outside:
            try:
                os.chdir(outside)
                paths.clear_cache()
                yield
            finally:
                os.chdir(saved)
                paths.clear_cache()

    def _reports(self, main, argv: list[str]) -> str:
        stderr = io.StringIO()
        with (
            self._no_project(),
            mock.patch.object(sys, "argv", argv),
            contextlib.redirect_stderr(stderr),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 2)
        return stderr.getvalue()

    def test_the_query_tool_names_the_missing_project(self) -> None:
        reported = self._reports(_qlog().main, ["qlog.py", "-n", "1"])
        self.assertIn("no project root found", reported)
        self.assertIn("agents-live init", reported)

    def test_the_timeline_names_the_missing_project(self) -> None:
        reported = self._reports(_timeline().main, ["timeline.py"])
        self.assertIn("no project root found", reported)
        self.assertIn("agents-live init", reported)


class _Relation:
    """What ``qlog.show`` uses of a DuckDB relation."""

    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self.columns = columns
        self._rows = rows
        self.shown = False

    def fetchall(self) -> list[tuple]:
        return self._rows

    def show(self, **kwargs) -> None:
        self.shown = True


class _Terminal(io.StringIO):
    """A captured stream that claims to be a console."""

    def isatty(self) -> bool:
        return True


def _qlog():
    """The query tool, imported in whichever layout is installed."""
    try:  # installed package layout, as at the top of this file
        from agents_live import qlog  # type: ignore  # noqa: PLC0415
    except ImportError:  # flat checkout layout
        import qlog  # type: ignore[no-redef]  # noqa: PLC0415
    return qlog


def _timeline():
    """The timeline reader, imported in whichever layout is installed."""
    try:  # installed package layout, as at the top of this file
        from agents_live import timeline  # type: ignore  # noqa: PLC0415
    except ImportError:  # flat checkout layout
        import timeline  # type: ignore[no-redef]  # noqa: PLC0415
    return timeline


class TestLogsReachTheirReader(_TempProject):
    """What the logs have to carry for a person to reach a conclusion."""

    def _render(self, relation: _Relation) -> str:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            _qlog().show(relation)
        return captured.getvalue()

    def test_a_piped_table_survives_the_shell_that_captures_it(self) -> None:
        # DuckDB's box drawing is UTF-8, and a Windows console at its
        # default codepage decodes a captured pipe as OEM bytes, so the
        # sanctioned way to read runtime state turns to noise exactly
        # when the reader is a program (#186). unittest replaces stdout
        # with a buffer, so this is the piped case by construction.
        relation = _Relation(["phase", "n"], [("one", 2), ("two", 3)])
        rendered = self._render(relation)
        self.assertFalse(relation.shown)
        self.assertTrue(rendered.isascii(), rendered)
        self.assertIn("phase", rendered)
        self.assertIn("(2 rows)", rendered)

    def test_a_terminal_still_gets_the_drawn_table(self) -> None:
        # The box-drawn table is better to look at, and it renders
        # correctly when it is written to a console rather than decoded
        # from one.
        relation = _Relation(["phase"], [("one",)])
        with contextlib.redirect_stdout(_Terminal()):
            _qlog().show(relation)
        self.assertTrue(relation.shown)

    def test_a_cell_cannot_break_the_row_it_is_in(self) -> None:
        # Messages carry tracebacks and agent output. A newline inside a
        # cell would put one row on several lines, which is unreadable
        # by eye and unparseable by anything else.
        rendered = self._render(_Relation(["message"], [("a\nb",), (None,)]))
        self.assertEqual(len(rendered.splitlines()), 5)  # 2 head, 2 rows, count

    def test_an_abandoned_attempt_records_the_time_it_spent(self) -> None:
        # A run rescued by a retry reports one aggregate duration, and
        # reading it as one slow call produced a wrong timeout budget
        # (#183). The numbers ride on the exception too, so a caller
        # deciding whether a failure is the host or the code does not
        # parse the sentence.
        error = headless.AgentTimeoutError(
            "agent timed out", attempts=2, timeout_s=120)
        self.assertEqual(error.attempts, 2)
        self.assertEqual(error.timeout_s, 120)
        self.assertEqual(error.category, "timeout")
        self.assertEqual(headless.AgentTimeoutError("x").attempts, 1)

    def test_a_completed_run_says_how_many_attempts_it_took(self) -> None:
        # The count is on the warning row and was missing from the row
        # that completes the run, so a finished run did not say whether
        # it took one call or two.
        result = headless._agent_result(
            "output", "", {}, None, None, attempts=2)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(headless.AgentResult("out", "").attempts, 1)

    def test_an_exhausted_retry_is_named_as_a_host_limit(self) -> None:
        # No wait budget can rescue a run the framework has stopped
        # retrying, so the gate correctly fails - and then reports the
        # link the same way it reports a defect (#185).
        started = time.time()
        name = smoketest.SMOKETEST_AGENT_NAMES[0]
        headless.log_event(
            headless.logs_root() / f"{name}.log",
            phase="agent", level="error",
            message="agent timed out after 120s on retry; giving up",
            error_category=headless.AgentTimeoutError.category,
            timeout_s=120, attempt=2, attempts=2, duration_s=120.0)
        reason = smoketest.exhausted_retries(started)
        self.assertIsNotNone(reason)
        self.assertIn(name, reason)
        self.assertIn("2 attempts", reason)
        self.assertIn("120s", reason)

    def test_an_older_timeout_does_not_condemn_this_run(self) -> None:
        # The classifier reads a log that outlives any one run, so a
        # timeout from an earlier session must not excuse a real failure
        # in this one.
        name = smoketest.SMOKETEST_AGENT_NAMES[0]
        headless.log_event(
            headless.logs_root() / f"{name}.log",
            phase="agent", level="error", message="agent timed out",
            error_category=headless.AgentTimeoutError.category,
            timeout_s=120, attempts=2)
        self.assertIsNone(smoketest.exhausted_retries(time.time() + 60))

    def test_a_clean_log_leaves_the_failure_where_it_was(self) -> None:
        self.assertIsNone(smoketest.exhausted_retries(time.time() - 60))


class TestCapabilityProbesAreObservable(_TempProject):
    """A probe that refuses or dawdles has to leave a trace."""

    def _admin_events(self) -> list[dict]:
        path = adminlog.log_path()
        if not path.is_file():
            return []
        return [json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_a_refused_probe_is_written_down(self) -> None:
        # The preflight ran two minutes of task-store queries on a
        # managed host and nothing recorded it, because this module
        # writes no events at all (#191). A refusal is now a row with
        # the capability, the code, and what it cost.
        refusal = preflight.CapabilityFailure(
            "host_permission_required", "schedule", "start", "no")
        with mock.patch.dict(preflight._CAPABILITY_PROBES,
                             {"schedule": lambda operation: refusal}):
            self.assertIs(preflight.check("start", {"schedule"}), refusal)
        recorded = [event for event in self._admin_events()
                    if event.get("operation") == "capability-probe"]
        self.assertEqual(len(recorded), 1, recorded)
        self.assertEqual(recorded[0]["capability"], "schedule")
        self.assertEqual(recorded[0]["needed_by"], "start")
        self.assertEqual(recorded[0]["error_category"],
                         "host_permission_required")
        self.assertIn("duration_s", recorded[0])

    def test_a_slow_probe_is_written_down_even_when_it_passes(self) -> None:
        def slow(operation: str) -> None:
            return None

        with (
            mock.patch.dict(preflight._CAPABILITY_PROBES, {"watch": slow}),
            mock.patch.object(preflight, "SLOW_PROBE_S", 0.0),
        ):
            self.assertIsNone(preflight.check("start", {"watch"}))
        recorded = [event for event in self._admin_events()
                    if event.get("operation") == "capability-probe"]
        self.assertEqual(len(recorded), 1, recorded)
        self.assertEqual(recorded[0]["status"], "ok")

    def test_an_ordinary_dispatch_stays_silent(self) -> None:
        # Every host-mutating command runs this. A row per invocation
        # would bury the one that matters.
        with mock.patch.dict(preflight._CAPABILITY_PROBES,
                             {"watch": lambda operation: None}):
            self.assertIsNone(preflight.check("start", {"watch"}))
        self.assertEqual(
            [event for event in self._admin_events()
             if event.get("operation") == "capability-probe"], [])


class TestUpgradeNamesWhatItLeftRunning(_TempProject):
    """An upgrade must not leave version skew behind in silence (#188)."""

    def setUp(self) -> None:
        super().setUp()
        self._other = tempfile.TemporaryDirectory()
        self.addCleanup(self._other.cleanup)
        self.outside = Path(self._other.name).resolve()

    def watcher_command(self, name: str, root: Path,
                        interpreter: str | None = None) -> str:
        """A packaged watch loop's command line, joined as its host joins."""
        argv = [] if interpreter is None else [interpreter]
        argv += ["agents-live", "--repo", str(root),
                 "internal", "watch-loop", name]
        if sys.platform == "win32":
            return subprocess.list2cmdline(argv)
        return " ".join(argv)

    def processes(self) -> list[tuple[int, str]]:
        """Two watchers in different projects, and one process that is not
        a watcher at all."""
        return [
            (1001, self.watcher_command("inbox-triage", self.root)),
            (1002, self.watcher_command("notes-sync", self.outside)),
            (1003, f"agents-live --repo {self.outside} status"),
        ]

    def _watchers(self):
        with mock.patch.object(hostruntime, "process_command_lines",
                               return_value=self.processes()):
            return headless.watchers_on_host()

    def test_every_project_is_enumerated_not_just_this_one(self) -> None:
        # The per-repo enumeration answers "what is running here", which
        # is the wrong question for a host-global tool environment.
        self.assertEqual(
            self._watchers(),
            [(1001, "inbox-triage", str(self.root)),
             (1002, "notes-sync", str(self.outside))])

    def test_a_flat_watcher_is_reported_without_guessing_its_project(
            self) -> None:
        # A flat checkout carries a script path at no fixed depth inside
        # the project, so naming a project from it would be a guess.
        with mock.patch.object(
                hostruntime, "process_command_lines",
                return_value=[(7, "/usr/bin/python3 /srv/x/y/activate.py "
                                  "watch-loop nightly")]):
            self.assertEqual(headless.watchers_on_host(),
                             [(7, "nightly", None)])

    def test_a_project_path_with_a_space_is_named_whole(self) -> None:
        if sys.platform != "win32":
            self.skipTest("only Windows reads command lines back with quoting")
        spaced = self.root / "a project"
        with mock.patch.object(
                hostruntime, "process_command_lines",
                return_value=[(11, self.watcher_command("todo", spaced))]):
            self.assertEqual(headless.watchers_on_host(),
                             [(11, "todo", str(spaced))])

    def test_the_upgrade_names_the_watchers_it_left_behind(self) -> None:
        end: dict = {}
        with (
            mock.patch.object(hostruntime, "is_alive",
                              side_effect=lambda pid: pid == 1001),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            upgrade._report_stale_watchers(self._watchers(), end)
        reported = stderr.getvalue()
        self.assertIn("inbox-triage", reported)
        self.assertIn("1001", reported)
        self.assertIn(str(self.root), reported)
        # The one that exited during the upgrade is not skew.
        self.assertNotIn("notes-sync", reported)
        self.assertIn("agents-live --repo", reported)
        self.assertEqual(end["stale_watchers"], 1)
        self.assertEqual(end["stale_watcher_agents"], "inbox-triage")

    def test_one_watcher_is_reported_once_however_many_processes_it_is(
            self) -> None:
        # Observed on Windows: a watcher is a shim plus the interpreter
        # it executes, so the process table shows the same agent two or
        # three times. Counting processes would tell an operator three
        # agents are stale when one is.
        processes = [
            (2001, self.watcher_command("notes", self.root)),
            (2002, self.watcher_command("notes", self.root,
                                        interpreter="python")),
        ]
        end: dict = {}
        with (
            mock.patch.object(hostruntime, "process_command_lines",
                              return_value=processes),
            mock.patch.object(hostruntime, "is_alive", return_value=True),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            upgrade._report_stale_watchers(headless.watchers_on_host(), end)
        self.assertEqual(end["stale_watchers"], 1)
        self.assertEqual(end["stale_watcher_agents"], "notes")
        reported = stderr.getvalue()
        self.assertIn("2001, 2002", reported)
        self.assertEqual(reported.count("notes ("), 1, reported)

    def test_an_upgrade_with_nothing_running_says_nothing(self) -> None:
        end: dict = {}
        with (
            mock.patch.object(hostruntime, "is_alive", return_value=False),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            upgrade._report_stale_watchers(self._watchers(), end)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(end["stale_watchers"], 0)

    def test_the_upgrade_records_the_skew_where_it_can_be_found_later(
            self) -> None:
        # The report is on the terminal of whoever ran the upgrade; the
        # symptom shows up days later, to someone else.
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            mock.patch.object(shutil, "which", return_value="/usr/bin/uv"),
            mock.patch.object(subprocess, "run", return_value=completed),
            mock.patch.object(plugins, "converge", return_value=False),
            mock.patch.object(hostruntime, "process_command_lines",
                              return_value=self.processes()),
            mock.patch.object(hostruntime, "is_alive", return_value=True),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(upgrade._upgrade_runtime(), 0)
        recorded = [json.loads(line) for line
                    in adminlog.log_path().read_text(
                        encoding="utf-8").splitlines() if line.strip()]
        upgrades = [event for event in recorded
                    if event.get("operation") == "upgrade-runtime"
                    and event.get("status") != "start"]
        self.assertEqual(len(upgrades), 1, recorded)
        self.assertEqual(upgrades[0]["stale_watchers"], 2)
        self.assertEqual(upgrades[0]["stale_watcher_agents"],
                         "inbox-triage, notes-sync")

    def test_an_unreadable_process_table_does_not_fail_the_upgrade(
            self) -> None:
        # Enumeration is a courtesy. An upgrade that worked must not be
        # reported as broken because the host would not list processes.
        with mock.patch.object(headless, "watchers_on_host",
                               side_effect=OSError("denied")):
            self.assertEqual(upgrade._running_watchers(), [])


if __name__ == "__main__":
    unittest.main()
