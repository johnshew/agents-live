#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML", "mcp[cli]", "jsonschema"]
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

import contextlib
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

try:  # installed package layout
    from agents_live import (  # type: ignore
        activate, agent_adapters, cli, completions, headless, health_check,
        heartbeat, init, migrate, ownership, paths, plugins, preflight,
        doctor, repos, spawn, state_migration, status, uninstall,
        update_check, upgrade,
    )
    from agents_live.cli_spec import (
        COMMANDS, GLOBAL_ARGS, HELP_ARG, POST_COMMAND_ARGS, render_docs_block,
        visible_args,
    )
except ImportError:  # flat checkout layout
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import activate
    import agent_adapters
    import cli
    import completions
    import headless
    import health_check
    import heartbeat
    import init
    import migrate
    import state_migration
    import ownership
    import paths
    import plugins
    import preflight
    import doctor
    import repos
    import spawn
    import status
    import update_check
    import upgrade
    import uninstall
    from cli_spec import (
        COMMANDS, GLOBAL_ARGS, HELP_ARG, POST_COMMAND_ARGS, render_docs_block,
        visible_args,
    )


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
        paths.clear_cache()

    def tearDown(self) -> None:
        if self._saved_env is None:
            os.environ.pop(paths.ENV_VAR, None)
        else:
            os.environ[paths.ENV_VAR] = self._saved_env
        if self._saved_state_home is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self._saved_state_home
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
FOREIGN_REPO = "/tmp/foreign-agents-live-project"


class TestSmoketestDispatch(_TempProject):
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
            mock.patch.object(smoketest, "_smoketest_process_tree",
                              return_value={}),
            mock.patch.object(smoketest, "_stop_process_tree", return_value=[]),
            mock.patch.object(smoketest.subprocess, "run", return_value=stopped),
            mock.patch.object(smoketest, "_smoketest_run_pids", return_value=[]),
            mock.patch.object(smoketest, "cron_is_active", return_value=False),
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

    def test_cli_default_registers_unregistered_path(self) -> None:
        self.assertEqual(repos.main(["default", str(self.root)]), 0)
        registry = repos.load()
        self.assertEqual(registry["repos"], {self.root.name: str(self.root)})
        self.assertEqual(registry["default_repo"], self.root.name)

    def test_add_reports_declared_plugins_without_installing(self) -> None:
        with (
            mock.patch.object(
                plugins, "checks",
                return_value=[("example-plugin", False, "not installed")]),
            mock.patch.object(plugins, "converge") as converge,
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(repos.main(["add", str(self.root)]), 0)
        converge.assert_not_called()
        self.assertIn(
            "will be installed on init/start/upgrade", stdout.getvalue())

    def test_add_registers_repo_when_plugin_check_fails(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(
                plugins, "checks",
                side_effect=plugins.PluginError("wheel is unreadable")),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(repos.main(["add", str(self.root)]), 0)
        registry = repos.load()
        self.assertEqual(registry["repos"], {self.root.name: str(self.root)})
        self.assertIn(
            "declared plugins will be installed on init/start/upgrade",
            stderr.getvalue(),
        )

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

    def test_default_is_last_resort(self) -> None:
        repos._add(str(self.root))
        repos._set_default(str(self.root))
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
        with self.assertRaisesRegex(ValueError, "repo-relative"):
            paths.validated_agent_directories(self.root, ["/tmp/agents"])
        with self.assertRaisesRegex(ValueError, "escapes"):
            paths.validated_agent_directories(self.root, ["../agents"])
        with tempfile.TemporaryDirectory() as outside:
            link = self.root / "linked-agents"
            link.symlink_to(outside, target_is_directory=True)
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


class TestStartOwnership(_TempProject):
    def setUp(self) -> None:
        super().setUp()
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        self.config = headless.load_agent_config("smoke-fixture")

    def _ownership_context(self):
        return (
            mock.patch.object(ownership, "local_only", return_value=False),
            mock.patch.object(ownership, "current_host",
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
            "smoke-fixture is owned by owning-host; "
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
            mock.patch.object(doctor, "_is_wsl", return_value=False),
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
        with (
            mock.patch.object(plugins, "converge", return_value=False) as converge,
            mock.patch.object(init, "install_skill", return_value=None),
            mock.patch("importlib.reload", return_value=mock.Mock(
                main=mock.Mock(return_value=0))),
            mock.patch("sys.argv", ["agents-live init"]),
            mock.patch("sys.stdout", new_callable=io.StringIO) as init_stdout,
        ):
            self.assertEqual(init.main(), 0)
        converge.assert_called_once_with([self.root])
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
        converge.assert_called_once_with([self.root])

        with (
            mock.patch.object(plugins, "converge") as converge,
            mock.patch.object(activate, "activate_one", return_value=["cron"]),
            mock.patch(
                "sys.argv",
                ["agents-live start", "--name", "smoke-fixture", "--dry-run"]),
        ):
            self.assertEqual(activate.main(), 0)
        converge.assert_not_called()

    def test_converge_treats_missing_wheel_as_converged_when_installed(self) -> None:
        wheel = self.root / "Agents" / "plugins" / "missing.whl"
        (self.root / ".agents-live.toml").write_text(
            f'[plugins]\nexample-plugin = {{ path = "{wheel.relative_to(self.root).as_posix()}" }}\n',
            encoding="utf-8",
        )
        entry_point = mock.Mock(
            group="agents_live.agents",
            name="example",
            load=mock.Mock(return_value=object()),
        )
        distribution = mock.Mock(version="1.2.3", entry_points=[entry_point])
        with mock.patch.object(
                plugins.importlib.metadata, "distribution",
                return_value=distribution):
            self.assertFalse(plugins.converge([self.root]))

    def test_converge_fails_when_missing_wheel_must_be_installed(self) -> None:
        wheel = self.root / "Agents" / "plugins" / "missing.whl"
        (self.root / ".agents-live.toml").write_text(
            f'[plugins]\nexample-plugin = {{ path = "{wheel.relative_to(self.root).as_posix()}" }}\n',
            encoding="utf-8",
        )
        with (
            mock.patch.object(
                plugins.importlib.metadata, "distribution",
                side_effect=plugins.importlib.metadata.PackageNotFoundError),
            self.assertRaisesRegex(plugins.PluginError, "wheel does not exist"),
        ):
            plugins.converge([self.root])


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
        self.assertTrue(activate.should_ignore_watch_change(
            self.root / "Agents" / "notes" / "_index_.md"))
        self.assertFalse(activate.should_ignore_watch_change(
            self.root / "Agents" / "notes" / "trigger.txt"))

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
    def test_run_invocation_carries_name_token(self) -> None:
        line = f"{TEST_CRON_SCHEDULE} cd {self.root} && " + " ".join(
            headless.run_invocation("t"))
        self.assertTrue(headless.cron_line_matches(line, "t"))

    def test_trigger_matching_is_scoped_to_current_repo(self) -> None:
        cron = (f"{TEST_CRON_SCHEDULE} cd {self.root} && agents-live run "
                "--name shared --quiet")
        watcher = headless.build_reboot_watcher_line("shared")
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
        cron = (f"{TEST_CRON_SCHEDULE} cd {self.root} && agents-live run "
                "--name shared --quiet")
        watcher = headless.build_reboot_watcher_line("shared")
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
        line = headless.build_reboot_watcher_line("t")
        self.assertIn("internal ensure-watcher t", line)
        self.assertNotIn("start --ensure-watcher", line)

    def test_persisted_lines_carry_inline_path(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        with mock.patch.object(activate, "_validate_handler_paths"):
            cron_lines = activate.build_cron_lines("smoke-fixture")
        watcher_line = headless.build_reboot_watcher_line("smoke-fixture")
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
            mock.patch.object(activate, "current_crontab_lines",
                              return_value=None),
            mock.patch.object(headless, "install_crontab") as h_install,
            mock.patch.object(activate, "install_crontab") as a_install,
            mock.patch.object(activate, "_validate_handler_paths"),
        ):
            with self.assertRaisesRegex(
                    headless.AgentsLiveError, "not accessible"):
                headless.install_watcher_reboot_line("smoke-fixture")
            with self.assertRaisesRegex(
                    headless.AgentsLiveError, "not accessible"):
                activate.install_cron_agent("smoke-fixture")
        h_install.assert_not_called()
        a_install.assert_not_called()

    def test_watcher_matching_is_scoped_to_current_repo(self) -> None:
        packaged = ["/home/u/.local/bin/agents-live", "--repo", str(self.root),
                    "internal", "watch-loop", "shared"]
        foreign = ["/home/u/.local/bin/agents-live", "--repo", FOREIGN_REPO,
                   "internal", "watch-loop", "shared"]
        flat = ["uv", "run", "--script",
                f"{self.root}/scripts/activate.py", "watch-loop", "shared"]
        self.assertTrue(headless._is_watcher_cmdline(packaged, "shared"))
        self.assertTrue(headless._is_watcher_cmdline(flat, "shared"))
        self.assertFalse(headless._is_watcher_cmdline(foreign, "shared"))
        self.assertEqual(
            headless._watcher_cmdline_agent_name(packaged), "shared")
        self.assertIsNone(headless._watcher_cmdline_agent_name(foreign))

    def test_packaged_cron_lines_are_enumerable(self) -> None:
        packaged = (f"{TEST_CRON_SCHEDULE} cd {self.root} && "
                    f"/home/u/.local/bin/agents-live --repo {self.root} "
                    "run --name foo --quiet 2>&1")
        flat = (f"{TEST_CRON_SCHEDULE} cd {self.root} && uv run --script "
                f"{self.root}/scripts/run.py --name bar --quiet 2>&1")
        unrelated = f"{TEST_CRON_SCHEDULE} cd {self.root} && /usr/bin/backup"
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
            f"{TEST_CRON_SCHEDULE} cd {gone} && agents-live --repo {gone} "
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
            mock.patch.object(activate, "current_crontab_lines",
                              return_value=[user_path, foreign]),
            mock.patch.object(activate, "install_crontab") as install,
            mock.patch.object(activate, "_validate_handler_paths"),
        ):
            activate.install_cron_agent("smoke-fixture")
        installed = install.call_args[0][0]
        self.assertIn(user_path, installed)
        self.assertIn(foreign, installed)


class TestMigratePlanning(_TempProject):
    def test_canonical_lines_are_no_op(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        canonical = activate.build_cron_lines("smoke-fixture")
        plan = migrate.plan_migration(canonical)
        self.assertEqual(plan["schedule"], {})
        self.assertEqual(plan["missing"], [])

    def test_stale_line_planned_for_rewrite(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        stale = (f"{TEST_CRON_SCHEDULE} cd {self.root} && "
                 f"/usr/bin/uv run --script "
                 f"{self.root}/old/run.py --name smoke-fixture --quiet 2>&1")
        plan = migrate.plan_migration([stale])
        self.assertIn("smoke-fixture", plan["schedule"])

    def test_legacy_watcher_line_is_planned_for_internal_rewrite(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        stale = (
            f"@reboot cd {self.root} && agents-live --repo {self.root} "
            "start --ensure-watcher smoke-fixture 2>&1"
        )
        plan = migrate.plan_migration([stale])
        old, new = plan["watcher"]["smoke-fixture"]
        self.assertEqual(old, [stale])
        self.assertIn("internal ensure-watcher smoke-fixture", new[0])

    def test_undefined_agent_is_reported_not_planned(self) -> None:
        line = (f"{TEST_CRON_SCHEDULE} cd {self.root} && uv run --script x.py "
                f"--name ghost-agent --quiet 2>&1")
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
        old_schedule = (
            f"{TEST_CRON_SCHEDULE} cd {old_root} && agents-live "
            f"--repo {old_root} run --name smoke-fixture --quiet 2>&1")
        old_watcher = (
            f"@reboot cd {old_root} && agents-live --repo {old_root} "
            "internal ensure-watcher smoke-fixture 2>&1")
        undefined = (
            f"{TEST_CRON_SCHEDULE} cd {old_root} && agents-live "
            f"--repo {old_root} run --name missing-agent --quiet 2>&1")
        near_match = old_schedule.replace(str(old_root), f"{old_root}-other")
        mixed_live = old_schedule.replace(
            f"--repo {old_root}", f"--repo {self.root}")
        live_entry = activate.build_cron_lines("smoke-fixture")[0]
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
            f"{TEST_CRON_SCHEDULE} cd {old_root} && agents-live "
            f"--repo {old_root} run --name smoke-fixture --quiet 2>&1")
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
            f"@reboot cd {foreign_repo} && agents-live --repo {foreign_repo} "
            "start --ensure-watcher missing",
            f"@reboot cd {self.root} && agents-live --repo {self.root} "
            "internal ensure-watcher smoke-fixture",
        ])
        completed = subprocess.CompletedProcess(
            ["crontab", "-l"], 0, stdout=crontab, stderr="")
        with (
            mock.patch.object(doctor, "REPO", self.root),
            mock.patch.object(doctor.subprocess, "run", return_value=completed),
        ):
            self.assertEqual(doctor._crontab_inconsistencies(), ([], []))


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

    def test_required_root_commands_emit_no_project_envelope(self) -> None:
        saved_cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as outside:
                os.chdir(outside)
                environ = {
                    key: value for key, value in os.environ.items()
                    if key not in (paths.ENV_VAR, "AGENTS_LIVE_JSON")
                }
                environ["XDG_CONFIG_HOME"] = str(
                    Path(outside) / "isolated-config")
                with mock.patch.dict(os.environ, environ, clear=True):
                    for command in COMMANDS:
                        if command.root != "required":
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

    def test_completions_help_explains_installation(self) -> None:
        for argv in (["completions", "help"], ["completions", "--help"],
                     ["help", "completions"]):
            with (
                self.subTest(argv=argv),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(cli.main(argv), 0)
                self.assertIn(
                    "~/.local/share/bash-completion/completions/agents-live",
                    stdout.getvalue(),
                )
                self.assertIn("~/.zfunc/_agents-live", stdout.getvalue())

    def test_repos_list_and_migrate_expose_structured_results(self) -> None:
        config_home = self.root / "contract-config"
        with mock.patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
            for argv, expected_key in (
                (["repos", "list", "--json"], "repositories"),
                (["migrate", "--dry-run", "--json"], "plan"),
            ):
                stdout = io.StringIO()
                with (
                    self.subTest(argv=argv),
                    mock.patch.object(preflight, "check", return_value=None),
                    mock.patch.object(
                        headless, "current_crontab_lines", return_value=[]),
                    contextlib.redirect_stdout(stdout),
                ):
                    self.assertEqual(cli.main(argv), 0)
                payload = json.loads(stdout.getvalue())
                self.assertTrue(payload["ok"])
                self.assertIn(expected_key, payload)

    def test_version_works_outside_repository(self) -> None:
        saved = Path.cwd()
        selected_root = os.environ.pop(paths.ENV_VAR, None)
        paths.clear_cache()
        try:
            with tempfile.TemporaryDirectory() as outside:
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
            mock.patch("sys.stdout", stdout),
        ):
            self.assertEqual(cli.main(["upgrade", "--skills-only"]), 0)
        install.assert_called_once_with(self.root)
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
            mock.patch.object(paths, "resolve_root") as resolve_root,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(cli.main(["upgrade"]), 0)
        runtime.assert_called_once_with([])
        resolve_root.assert_not_called()

    def test_doctor_without_project_root_runs_host_checks(self) -> None:
        os.environ.pop(paths.ENV_VAR, None)
        paths.clear_cache()
        with (
            mock.patch.object(doctor, "REPO", None),
            mock.patch.object(doctor, "_has", return_value=True),
            mock.patch.object(doctor, "_python_312_resolvable", return_value=True),
            mock.patch.object(doctor, "_is_wsl", return_value=False),
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

    def test_rootless_all_repos_dashboard_has_no_relative_paths(self) -> None:
        dashboard = Path(headless.__file__).with_name("dashboard.py")
        code = f"""
import importlib.util
import sys
import types
from unittest import mock
from agents_live import headless, repos

nicegui = types.ModuleType("nicegui")
nicegui.app = mock.MagicMock()
nicegui.ui = mock.MagicMock()
nicegui.run = mock.MagicMock()
sys.modules["nicegui"] = nicegui
headless.repo_root = mock.Mock(side_effect=ValueError("no root"))
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

    def test_doctor_forces_refresh_and_ignores_io_failure(self) -> None:
        with (
            mock.patch.object(doctor, "collect", return_value=[]),
            mock.patch.object(doctor, "_hostname", return_value="test-host"),
            mock.patch.object(
                update_check, "refresh", side_effect=OSError) as refresh,
            mock.patch.object(
                update_check, "status_text", return_value="Update check: current") as status,
            mock.patch.object(update_check, "interactive", return_value=True),
            mock.patch("sys.stdout", io.StringIO()),
            mock.patch("sys.stderr", io.StringIO()),
        ):
            self.assertEqual(doctor.main([]), 0)
        refresh.assert_called_once()
        status.assert_called_once()

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
        refresh.assert_called_once()
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
            mock.patch.object(heartbeat, "is_wsl", return_value=True),
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
            mock.patch.object(heartbeat, "is_wsl", return_value=False),
            mock.patch.object(heartbeat, "uninstall") as host_cleanup,
            mock.patch.object(uninstall.health_check,
                              "remove_health_cron_lines",
                              return_value=True) as remove_loop,
            mock.patch.object(uninstall, "find_uv",
                              return_value="/usr/bin/uv"),
            mock.patch.object(uninstall.subprocess, "run",
                              return_value=completed) as uv_uninstall,
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(uninstall.main([]), 0)
        host_cleanup.assert_not_called()
        remove_loop.assert_called_once_with()
        uv_uninstall.assert_called_once_with(
            ["/usr/bin/uv", "tool", "uninstall", "agents-live"], check=False)

    def test_install_refuses_cross_distro_target(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            heartbeat.install("Debian")

    def test_uninstall_removes_crontab_lock(self) -> None:
        directory = heartbeat.state_dir()
        directory.mkdir(parents=True)
        (directory / "crontab.lock").touch()
        with mock.patch.object(heartbeat, "_task_exists", return_value=False):
            heartbeat.uninstall()
        self.assertFalse((directory / "crontab.lock").exists())

    def test_doctor_accepts_stable_distro_task(self) -> None:
        execute, arguments = heartbeat.task_action("Ubuntu", self.shim)
        # The action must launch through the hidden-window wrapper, not
        # bare wsl.exe (which flashes a console every five minutes).
        self.assertEqual(execute, "wscript.exe")
        self.assertIn("run-hidden.vbs", arguments)
        self.assertIn(r"\\wsl.localhost\Ubuntu", arguments)
        self.assertIn("wsl.exe", arguments)
        task = {
            "Enabled": True,
            "Execute": execute,
            "Arguments": arguments,
            "Interval": "PT5M",
        }
        with mock.patch.object(
                heartbeat, "task_configuration", return_value=(task, False)):
            self.assertEqual(
                doctor._windows_heartbeat_config(),
                (True, "enabled; distro Ubuntu; hidden stable CLI shim; "
                       "repeats every 5 min"))

    def test_doctor_flags_visible_console_registration(self) -> None:
        # Pre-0.3.1 tasks executed wsl.exe directly; doctor must point
        # at the reinstall that wraps them with run-hidden.vbs.
        task = {
            "Enabled": True,
            "Execute": "wsl.exe",
            "Arguments": heartbeat.wsl_command("Ubuntu", self.shim),
            "Interval": "PT5M",
        }
        with mock.patch.object(
                heartbeat, "task_configuration", return_value=(task, False)):
            ok, note = doctor._windows_heartbeat_config()
        self.assertFalse(ok)
        self.assertIn("visible console", note)
        self.assertIn("heartbeat install", note)

    def test_doctor_recommends_migration_for_legacy_task(self) -> None:
        with mock.patch.object(
                heartbeat, "task_configuration", return_value=(None, True)):
            ok, note = doctor._windows_heartbeat_config()
        self.assertFalse(ok)
        self.assertIn("requires migration", note)

    def test_compatibility_wrapper_executes_automatic_migration(self) -> None:
        wrapper = Path(heartbeat.__file__).with_name("windows-heartbeat.sh")
        invocation = self.root / "invocation"
        self.shim.write_text(
            f"#!/bin/sh\nprintf '%s\\n' \"$*\" > {invocation}\n",
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
        (xdg / "agents-live" / "config.toml").write_text(
            f'default_repo = "proj"\n\n[repos]\nproj = "{self.root}"\n',
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
            f'default_repo = "proj"\n\n[repos]\nproj = "{self.root}"\n',
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
        with mock.patch.object(update_check.subprocess, "Popen") as popen:
            update_check.launch_if_stale(now=100)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0][2], update_check.__name__)

        update_check.refresh(
            now=100,
            opener=mock.Mock(return_value=self._response({
                "info": {"version": "1.2.3"},
            })),
        )
        with mock.patch.object(update_check.subprocess, "Popen") as popen:
            update_check.launch_if_stale(now=101)
        popen.assert_not_called()

        with mock.patch.object(update_check.subprocess, "Popen") as popen:
            update_check.launch_if_stale(now=100 + update_check.CACHE_INTERVAL - 1)
        popen.assert_not_called()

        with mock.patch.object(update_check.subprocess, "Popen") as popen:
            update_check.launch_if_stale(now=100 + update_check.CACHE_INTERVAL)
        popen.assert_called_once()

    def test_legacy_opt_outs_do_not_suppress_check(self) -> None:
        config = Path(os.environ["XDG_CONFIG_HOME"]) / "agents-live" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("update_check = false\n", encoding="utf-8")
        with (
            mock.patch.dict(os.environ, {"AGENTS_LIVE_NO_UPDATE_CHECK": "1"}),
            mock.patch.object(update_check.subprocess, "Popen") as popen,
        ):
            update_check.launch_if_stale(now=100)
        popen.assert_called_once()

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
                str(path.relative_to(module.ROOT)) for path in module.RELEASE_FILES)

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
            module.CHANGELOG.write_text(
                "# Changelog\n\n## Unreleased\n\n## 1.2.3 - 2026-07-18\n\n"
                "- fix: keep this standalone summary.\n"
                "  This detail stays in the changelog.\n"
                "- feat!: replace the old contract.\n"
                "  Migration details also stay out of the release body.\n",
                encoding="utf-8",
            )
            existing = subprocess.CompletedProcess(
                args=[], returncode=0, stdout='{"url":"https://example.test/release"}\n')
            existing_body = (
                "- fix: keep this standalone summary.\n\n"
                "[Full changelog](https://github.com/johnshew/agents-live/"
                "blob/v1.2.3/src/agents_live/skill/docs/changelog.md)\n\n"
                "## What's Changed\n"
                "* Fix a bug in https://example.test/pull/1\n\n"
                "**Diffs**: https://github.com/johnshew/agents-live/"
                "compare/v1.2.2...v1.2.3"
            )
            expected_body = (
                "## Curated Summary\n\n"
                "- fix: keep this standalone summary.\n\n"
                "## What's Changed\n"
                "* Fix a bug in https://example.test/pull/1\n\n"
                "[Full changelog](https://github.com/johnshew/agents-live/"
                "blob/v1.2.3/src/agents_live/skill/docs/changelog.md) | "
                "[v1.2.2...v1.2.3](https://github.com/johnshew/agents-live/"
                "compare/v1.2.2...v1.2.3)\n"
            )
            edited_bodies = []

            def capture_existing_run(argv, *, capture=False):
                if argv[:3] == ["gh", "release", "view"]:
                    return existing_body
                if argv[:3] == ["gh", "release", "edit"]:
                    notes_path = Path(argv[argv.index("--notes-file") + 1])
                    edited_bodies.append(notes_path.read_text(encoding="utf-8"))
                return ""

            with (
                mock.patch.object(module, "_require_tools"),
                mock.patch.object(module, "_check_publish_state", return_value=False),
                mock.patch.object(module.subprocess, "run", return_value=existing),
                mock.patch.object(
                    module, "_run", side_effect=capture_existing_run
                ) as run,
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                module.publish()
            commands = [call.args[0] for call in run.call_args_list]
            self.assertFalse(any(command[0] == "uv" for command in commands))
            self.assertEqual(edited_bodies, [expected_body])

            missing = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
            release_bodies = []
            edited_bodies.clear()
            generated_body = (
                "## Curated Summary\n\n"
                "- fix: keep this standalone summary.\n"
                "- feat!: replace the old contract.\n\n"
                "## What's Changed\n"
                "* Fix a bug in https://example.test/pull/1\n\n"
                "**Full Changelog**: https://github.com/johnshew/agents-live/"
                "compare/v1.2.2...v1.2.3"
            )

            def capture_run(argv, *, capture=False):
                if argv[:3] == ["gh", "release", "create"]:
                    notes_path = Path(argv[argv.index("--notes-file") + 1])
                    release_bodies.append(notes_path.read_text(encoding="utf-8"))
                if argv[:3] == ["gh", "release", "view"]:
                    return generated_body
                if argv[:3] == ["gh", "release", "edit"]:
                    notes_path = Path(argv[argv.index("--notes-file") + 1])
                    edited_bodies.append(notes_path.read_text(encoding="utf-8"))
                return ""

            with (
                mock.patch.object(module, "_require_tools"),
                mock.patch.object(module, "_check_publish_state", return_value=False),
                mock.patch.object(module.subprocess, "run", return_value=missing),
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
            self.assertEqual(
                release_bodies,
                [
                "## Curated Summary\n\n"
                "- fix: keep this standalone summary.\n"
                "- feat!: replace the old contract.\n"
                ],
            )
            self.assertEqual(
                edited_bodies,
                [
                "## Curated Summary\n\n"
                "- fix: keep this standalone summary.\n"
                "- feat!: replace the old contract.\n\n"
                "## What's Changed\n"
                "* Fix a bug in https://example.test/pull/1\n\n"
                "[Full changelog](https://github.com/johnshew/agents-live/"
                "blob/v1.2.3/src/agents_live/skill/docs/changelog.md) | "
                "[v1.2.2...v1.2.3](https://github.com/johnshew/agents-live/"
                "compare/v1.2.2...v1.2.3)\n"
                ],
            )

            edited_bodies.clear()

            def capture_normalized_run(argv, *, capture=False):
                if argv[:3] == ["gh", "release", "view"]:
                    return expected_body.rstrip()
                if argv[:3] == ["gh", "release", "edit"]:
                    edited_bodies.append(argv)
                return ""

            with mock.patch.object(
                module, "_run", side_effect=capture_normalized_run
            ):
                module._normalize_release_body("v1.2.3")
            self.assertEqual(edited_bodies, [])


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
            mock.patch.object(init, "install_skill", return_value="refreshed"),
            mock.patch("builtins.print") as output,
            mock.patch("sys.argv", ["agents-live upgrade", "--skills-only"]),
        ):
            self.assertEqual(upgrade.main(), 0)
        output.assert_any_call(
            f"{self.root}: upgraded skill payload to match the installed package")
        output.assert_any_call(
            f"Installed agents-live version: {upgrade.__version__}")

        with (
            mock.patch.object(init, "install_skill", return_value=None),
            mock.patch("builtins.print") as output,
            mock.patch("sys.argv", ["agents-live upgrade", "--skills-only"]),
        ):
            self.assertEqual(upgrade.main(), 0)
        output.assert_any_call(
            f"{self.root}: skill payload already matches the installed package")

    def test_runtime_upgrade_preserves_receipt_and_converges_plugins(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            mock.patch.object(shutil, "which", return_value="/usr/bin/uv"),
            mock.patch.object(subprocess, "run", return_value=completed) as run,
            mock.patch.object(plugins, "converge", return_value=False) as converge,
        ):
            self.assertEqual(upgrade._upgrade_runtime(), 0)
        run.assert_called_once_with(
            ["/usr/bin/uv", "tool", "upgrade", "agents-live"],
            check=False,
        )
        converge.assert_called_once_with([])

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
                tool_python = root / "tools" / "agents-live" / "bin" / "python"
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
                plugins, "inspect",
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
        runtime.assert_called_once_with([target])
        refresh.assert_called_once_with()
        install.assert_not_called()

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
            mock.patch("sys.stdout", new_callable=io.StringIO),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            mock.patch("sys.argv", ["agents-live upgrade", "--skills-only"]),
        ):
            self.assertEqual(upgrade.main(), 1)
        self.assertEqual(
            refresh.call_args_list,
            [mock.call(broken), mock.call(healthy)],
        )
        self.assertIn("broken (/repos/broken): denied", stderr.getvalue())


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

    def test_state_migration_moves_legacy_state(self) -> None:
        legacy_logs = self.root / "Agents" / "logs"
        (legacy_logs / "archive").mkdir(parents=True)
        (legacy_logs / "demo.log").write_text("{}\n", encoding="utf-8")
        (legacy_logs / "archive" / "old.parquet").write_bytes(b"pq")
        data = self.root / "Agents" / "data"
        (data / "health.ok").write_text("{}\n", encoding="utf-8")
        (data / "smoketest-framework.lock").write_text("", encoding="utf-8")
        (data / "demo-watch-hashes.json").write_text("{}", encoding="utf-8")
        (data / "agent-owners.json").write_text("{}", encoding="utf-8")

        from_module = state_migration.apply(self.root)
        self.assertGreater(from_module, 0)

        state_dir = paths.repo_state_dir(self.root)
        self.assertTrue((state_dir / "logs" / "demo.log").is_file())
        self.assertTrue(
            (state_dir / "logs" / "archive" / "old.parquet").is_file())
        self.assertTrue((state_dir / "demo-watch-hashes.json").is_file())
        self.assertFalse((data / "health.ok").exists())
        self.assertFalse((data / "smoketest-framework.lock").exists())
        # Shared git-synced state stays in the tree.
        self.assertTrue((data / "agent-owners.json").is_file())
        # Emptied legacy directory is tidied away; second pass is a no-op.
        self.assertFalse(legacy_logs.exists())
        self.assertEqual(state_migration.apply(self.root), 0)

    def test_state_migration_appends_colliding_legacy_log(self) -> None:
        # Legacy content is appended (never a truncate-rewrite, which
        # would race concurrent appenders at the new home); qlog and
        # timeline order by timestamp, not file position. A legacy file
        # cut mid-write gains a terminating newline so records never fuse.
        legacy_logs = self.root / "Agents" / "logs"
        legacy_logs.mkdir(parents=True)
        (legacy_logs / "demo.log").write_text("old", encoding="utf-8")
        dest = paths.repo_state_dir(self.root) / "logs" / "demo.log"
        dest.parent.mkdir(parents=True)
        dest.write_text("new\n", encoding="utf-8")
        state_migration.apply(self.root)
        self.assertEqual(dest.read_text(encoding="utf-8"), "new\nold\n")
        self.assertFalse((legacy_logs / "demo.log").exists())


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
            self.assertIn("health-check --quiet", line)
            self.assertTrue(health_check.health_cron_line_matches(line))

    def test_matcher_ignores_legacy_agent_and_foreign_lines(self) -> None:
        legacy = ("0 * * * * cd /some/project && PATH=/usr/bin "
                  "/usr/local/bin/agents-live --repo /some/project run "
                  "--name agents-live-health-check --quiet 2>&1")
        self.assertFalse(health_check.health_cron_line_matches(legacy))
        self.assertFalse(health_check.health_cron_line_matches(
            "0 * * * * /usr/bin/backup health-check 2>&1"))

    def test_ensure_converges_and_respects_opt_in(self) -> None:
        installed: dict[str, list[str]] = {}
        foreign = "0 1 * * * /usr/bin/foreign-job 2>&1"
        stale = "@reboot PATH=/old /old/bin/agents-live health-check --quiet 2>&1"

        def fake_install(lines: list[str]) -> None:
            installed["lines"] = list(lines)

        with (
            mock.patch.object(health_check, "cli_shim_path",
                              return_value=_HEALTH_SHIM),
            mock.patch.object(health_check, "current_crontab_lines",
                              return_value=[foreign]),
            mock.patch.object(health_check, "install_crontab", fake_install),
        ):
            # Not installed + install=False: never adds (opt-in stays
            # with an explicit health-check run).
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
            mock.patch.object(health_check, "current_crontab_lines",
                              return_value=[stale, foreign]),
            mock.patch.object(health_check, "install_crontab", fake_install),
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
            mock.patch.object(health_check, "current_crontab_lines",
                              return_value=[foreign] + self._canonical_lines()),
            mock.patch.object(health_check, "install_crontab",
                              lambda lines: installed.update(lines=list(lines))),
        ):
            self.assertTrue(health_check.remove_health_cron_lines())
            self.assertEqual(installed["lines"], [foreign])
        with mock.patch.object(health_check, "current_crontab_lines",
                               return_value=[foreign]):
            self.assertFalse(health_check.remove_health_cron_lines())

    def test_sweep_reports_degraded_ownership_without_aborting(self) -> None:
        # The 2026-07-19 incident class: ownership unavailable must
        # degrade the sweep, never kill the loop.
        with (
            mock.patch.object(
                health_check.ownership, "load_owners",
                side_effect=health_check.ownership.OwnershipUnavailableError(
                    "no backend")),
            mock.patch.object(health_check, "_converge_crontab",
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
            mock.patch.object(activate, "prune_orphans", noisy_prune),
            mock.patch.object(health_check, "_converge_crontab",
                              return_value=True),
            mock.patch.object(health_check, "_origin_main_synced",
                              return_value=False),
            mock.patch.object(
                health_check.ownership, "load_owners",
                side_effect=health_check.ownership.OwnershipUnavailableError(
                    "no backend")),
            mock.patch.object(sys, "argv",
                              ["agents-live health-check", "--sweep"]),
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

    def test_host_command_is_declared(self) -> None:
        command = cli.COMMAND_BY_NAME["health-check"]
        self.assertEqual(command.module, "health_check")
        self.assertEqual(command.root, "none")
        self.assertIn("crontab", command.probes)


if __name__ == "__main__":
    unittest.main()
