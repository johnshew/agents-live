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
import importlib.metadata
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:  # installed package layout
    from agents_live import (  # type: ignore
        activate, agent_adapters, cli, headless, heartbeat, init, migrate,
        ownership, paths, prereqs, repos, spawn, status, uninstall,
        update_check, upgrade,
    )
except ImportError:  # flat checkout layout
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import activate
    import agent_adapters
    import cli
    import headless
    import heartbeat
    import init
    import migrate
    import ownership
    import paths
    import prereqs
    import repos
    import spawn
    import status
    import update_check
    import upgrade
    import uninstall


class _TempProject(unittest.TestCase):
    """A temp project selected via the env var, restored on teardown."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".agents-live.toml").write_text("", encoding="utf-8")
        (self.root / "Agents" / "data").mkdir(parents=True)
        (self.root / "Agents" / "logs").mkdir(parents=True)
        self._saved_env = os.environ.get(paths.ENV_VAR)
        os.environ[paths.ENV_VAR] = str(self.root)
        paths.clear_cache()

    def tearDown(self) -> None:
        if self._saved_env is None:
            os.environ.pop(paths.ENV_VAR, None)
        else:
            os.environ[paths.ENV_VAR] = self._saved_env
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


class TestAgentParsing(_TempProject):
    def test_native_agent_parses(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        config = headless.load_agent_config("smoke-fixture")
        self.assertEqual(config.name, "smoke-fixture")
        self.assertEqual(config.schedule, [TEST_CRON_SCHEDULE])

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
        for benign in ("@reboot", "*/5 8-18 * * 1-5", TEST_CRON_SCHEDULE):
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
        self.assertIn("--ensure-watcher", line)

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
                    "start", "--watch-loop", "shared"]
        foreign = ["/home/u/.local/bin/agents-live", "--repo", FOREIGN_REPO,
                   "start", "--watch-loop", "shared"]
        flat = ["uv", "run", "--script",
                f"{self.root}/scripts/activate.py", "--watch-loop", "shared"]
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
            mock.patch.object(prereqs, "REPO", self.root),
            mock.patch.object(prereqs.subprocess, "run",
                              return_value=completed),
        ):
            orphans, stale = prereqs._crontab_inconsistencies()
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
        with mock.patch.object(prereqs.subprocess, "run",
                               return_value=completed):
            self.assertIsNone(prereqs._crontab_inconsistencies())

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
            "start --ensure-watcher smoke-fixture",
        ])
        completed = subprocess.CompletedProcess(
            ["crontab", "-l"], 0, stdout=crontab, stderr="")
        with (
            mock.patch.object(prereqs, "REPO", self.root),
            mock.patch.object(prereqs.subprocess, "run", return_value=completed),
        ):
            self.assertEqual(prereqs._crontab_inconsistencies(), ([], []))


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
    def setUp(self) -> None:
        super().setUp()
        for patcher in (
            mock.patch.object(update_check, "consume_notice", return_value=None),
            mock.patch.object(update_check, "launch_if_stale"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_help_exits_zero(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
        ):
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertIn("usage: agents-live", stdout.getvalue())
        self.assertIn("upgrade", stdout.getvalue())
        self.assertIn("--version", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

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
        self.assertIn("error: unknown command 'frobnicate'", stderr.getvalue())
        self.assertIn("usage: agents-live", stderr.getvalue())

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
        runtime.assert_called_once_with()
        resolve_root.assert_not_called()

    def test_doctor_without_project_root_runs_host_checks(self) -> None:
        os.environ.pop(paths.ENV_VAR, None)
        paths.clear_cache()
        with (
            mock.patch.object(prereqs, "REPO", None),
            mock.patch.object(prereqs, "_has", return_value=True),
            mock.patch.object(prereqs, "_python_312_resolvable", return_value=True),
            mock.patch.object(prereqs, "_is_wsl", return_value=False),
            mock.patch.object(prereqs, "_hostname", return_value="test-host"),
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

    def test_doctor_forces_refresh_and_ignores_io_failure(self) -> None:
        with (
            mock.patch.object(prereqs, "collect", return_value=[]),
            mock.patch.object(prereqs, "_hostname", return_value="test-host"),
            mock.patch.object(
                update_check, "refresh", side_effect=OSError) as refresh,
            mock.patch.object(
                update_check, "status_text", return_value="Update check: current") as status,
            mock.patch.object(update_check, "interactive", return_value=True),
            mock.patch("sys.stdout", io.StringIO()),
            mock.patch("sys.stderr", io.StringIO()),
        ):
            self.assertEqual(prereqs.main([]), 0)
        refresh.assert_called_once()
        status.assert_called_once()

    def test_doctor_json_suppresses_cached_update_result(self) -> None:
        with (
            mock.patch.object(prereqs, "collect", return_value=[]),
            mock.patch.object(prereqs, "_hostname", return_value="test-host"),
            mock.patch.object(update_check, "refresh") as refresh,
            mock.patch.object(update_check, "status_text") as status,
            mock.patch.object(update_check, "interactive", return_value=True),
            mock.patch("sys.stdout", io.StringIO()),
            mock.patch("sys.stderr", io.StringIO()),
        ):
            self.assertEqual(prereqs.main(["--json"]), 0)
        refresh.assert_called_once()
        status.assert_not_called()

    def test_doctor_json_flag_positions_are_equivalent(self) -> None:
        def invoke(argv: list[str]) -> dict:
            stdout = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"AGENTS_LIVE_JSON": ""}),
                mock.patch.object(prereqs, "collect", return_value=[]),
                mock.patch.object(prereqs, "_hostname", return_value="test-host"),
                mock.patch.object(update_check, "refresh"),
                mock.patch("sys.stdout", stdout),
            ):
                self.assertEqual(cli.main(argv), 0)
            return json.loads(stdout.getvalue())

        self.assertEqual(
            invoke(["--json", "doctor"]),
            invoke(["doctor", "--json"]),
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
            mock.patch.object(uninstall.shutil, "which",
                              return_value="/usr/bin/uv"),
            mock.patch.object(uninstall.subprocess, "run",
                              return_value=completed) as uv_uninstall,
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(uninstall.main([]), 0)
        host_cleanup.assert_not_called()
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
                prereqs._windows_heartbeat_config(),
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
            ok, note = prereqs._windows_heartbeat_config()
        self.assertFalse(ok)
        self.assertIn("visible console", note)
        self.assertIn("heartbeat install", note)

    def test_doctor_recommends_migration_for_legacy_task(self) -> None:
        with mock.patch.object(
                heartbeat, "task_configuration", return_value=(None, True)):
            ok, note = prereqs._windows_heartbeat_config()
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


class TestTimeline(_TempProject):
    def test_bare_timeline_keeps_valid_rows_among_invalid_rows(self) -> None:
        log = self.root / "Agents" / "logs" / "mixed.log"
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
            self.assertEqual(cli._finish(7, "status", [], json_mode=False), 7)
            consume.assert_not_called()
            launch.assert_not_called()
        with (
            mock.patch.object(update_check, "interactive", return_value=True),
            mock.patch.object(update_check, "consume_notice") as consume,
            mock.patch.object(update_check, "launch_if_stale") as launch,
        ):
            cli._finish(0, "run", ["--quiet"], json_mode=False)
            cli._finish(0, "status", ["--json"], json_mode=False)
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
            root / "src" / "agents_live" / "cli.py",
            root / "src" / "agents_live" / "skill" / "VERSION",
        )
        module.CHANGELOG = (
            root / "src" / "agents_live" / "skill" / "docs" / "changelog.md")
        module.RELEASE_FILES = (
            module.PYPROJECT, *module.VERSION_FILES, module.CHANGELOG)
        contents = (
            'version = "1.2.3"\n',
            '__version__ = "1.2.3"\n',
            'blob = "https://example.test/blob/v1.2.3/docs"\n',
            "1.2.3\n",
            "# Changelog\n\n## Unreleased\n\nA fix.\n\n## 1.2.3\n\nOld.\n",
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

    def test_minimum_bump_detects_breaking_change_markers(self) -> None:
        module = self._load_tool()
        self.assertEqual(module._minimum_bump("- feat!: replace the API."), "major")
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
            existing = subprocess.CompletedProcess(
                args=[], returncode=0, stdout='{"url":"https://example.test/release"}\n')
            with (
                mock.patch.object(module, "_require_tools"),
                mock.patch.object(module, "_check_publish_state", return_value=False),
                mock.patch.object(module.subprocess, "run", return_value=existing),
                mock.patch.object(module, "_run") as run,
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                module.publish()
            run.assert_not_called()

            missing = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
            with (
                mock.patch.object(module, "_require_tools"),
                mock.patch.object(module, "_check_publish_state", return_value=False),
                mock.patch.object(module.subprocess, "run", return_value=missing),
                mock.patch.object(module, "_run") as run,
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                module.publish()
            commands = [call.args[0] for call in run.call_args_list]
            self.assertFalse(any(command[:2] == ["git", "push"] for command in commands))
            self.assertTrue(any(command[:3] == ["gh", "release", "create"]
                                for command in commands))


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

        with (
            mock.patch.object(init, "install_skill", return_value=None),
            mock.patch("builtins.print") as output,
            mock.patch("sys.argv", ["agents-live upgrade", "--skills-only"]),
        ):
            self.assertEqual(upgrade.main(), 0)
        output.assert_any_call(
            f"{self.root}: skill payload already matches the installed package")

    def test_runtime_upgrade_installs_unpinned_latest_release(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            mock.patch.object(shutil, "which", return_value="/usr/bin/uv"),
            mock.patch.object(subprocess, "run", return_value=completed) as run,
        ):
            self.assertEqual(upgrade._upgrade_runtime(), 0)
        run.assert_called_once_with(
            ["/usr/bin/uv", "tool", "install", "--force", "agents-live@latest"],
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
        ):
            self.assertEqual(upgrade.main(), 0)
        runtime.assert_called_once_with()
        refresh.assert_called_once_with(target)
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


if __name__ == "__main__":
    unittest.main()
