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

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:  # installed package layout
    from agents_live import (  # type: ignore
        activate, agent_adapters, cli, headless, init, migrate, ownership,
        paths, spawn,
    )
except ImportError:  # flat checkout layout
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import activate
    import agent_adapters
    import cli
    import headless
    import init
    import migrate
    import ownership
    import paths
    import spawn


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


AGENT_DEFINITION = """---
description: Smoke fixture. Never delegate to this agent.
disable-model-invocation: true
runtime: none
mode: plan
schedule: "0 6 * * *"
pre-processor: Agents/handlers/prep.py
---
Smoke fixture body.
"""


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
        self.assertEqual(config.schedule, ["0 6 * * *"])

    def test_unknown_runtime_fails_closed(self) -> None:
        self.write_agent("bad-runtime", AGENT_DEFINITION.replace("runtime: none",
                                 "runtime: nonsense"))
        with self.assertRaises(headless.AgentsLiveError):
            headless.load_agent_config("bad-runtime")


class TestInvocationForms(_TempProject):
    def test_run_invocation_carries_name_token(self) -> None:
        line = " ".join(headless.run_invocation("t"))
        self.assertTrue(headless.cron_line_matches(line, "t"))

    def test_reboot_line_round_trips_agent_name(self) -> None:
        line = headless.build_reboot_watcher_line("t")
        self.assertIn("--ensure-watcher", line)


class TestMigratePlanning(_TempProject):
    def test_canonical_lines_are_no_op(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        canonical = activate.build_cron_lines("smoke-fixture")
        plan = migrate.plan_migration(canonical)
        self.assertEqual(plan["schedule"], {})
        self.assertEqual(plan["missing"], [])

    def test_stale_line_planned_for_rewrite(self) -> None:
        self.write_agent("smoke-fixture", AGENT_DEFINITION)
        stale = (f"0 6 * * * cd {self.root} && /usr/bin/uv run --script "
                 f"{self.root}/old/run.py --name smoke-fixture --quiet 2>&1")
        plan = migrate.plan_migration([stale])
        self.assertIn("smoke-fixture", plan["schedule"])

    def test_undefined_agent_is_reported_not_planned(self) -> None:
        line = (f"0 6 * * * cd {self.root} && uv run --script x.py "
                f"--name ghost-agent --quiet 2>&1")
        plan = migrate.plan_migration([line])
        self.assertEqual(plan["schedule"], {})
        self.assertIn("ghost-agent", plan["missing"])


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
    def test_help_exits_zero(self) -> None:
        self.assertEqual(cli.main(["--help"]), 0)

    def test_unknown_command_exits_two(self) -> None:
        self.assertEqual(cli.main(["frobnicate"]), 2)

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

    def test_dry_run_reports_bump_without_modifying_version(self) -> None:
        root = Path(__file__).resolve().parents[1]
        pyproject = root / "pyproject.toml"
        before = pyproject.read_bytes()
        result = subprocess.run(
            ["uv", "run", "--script", str(root / "tools" / "release.py"),
             "--dry-run"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"Release plan: \d+\.\d+\.\d+ -> \d+\.\d+\.\d+")
        self.assertIn("git push --atomic", result.stdout)
        self.assertEqual(pyproject.read_bytes(), before)

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
            ):
                module.publish()
            run.assert_not_called()

            missing = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
            with (
                mock.patch.object(module, "_require_tools"),
                mock.patch.object(module, "_check_publish_state", return_value=False),
                mock.patch.object(module.subprocess, "run", return_value=missing),
                mock.patch.object(module, "_run") as run,
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
