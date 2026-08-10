#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML", "mcp[cli]<2", "jsonschema"]
# ///
"""Portable end-to-end smoke coverage for the 6.0 runtime seams."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents_live import agent, obs, paths, state
from agents_live.cli import main as cli_main
from agents_live.cli import lifecycle
from agents_live.cli.commands import init
from agents_live.cli.commands import status
from agents_live.dispatch import Firing, dispatch
from agents_live.runtime import (
    ChildResult,
    parse_watch,
)
from agents_live.runtime.hosts.memory import MemoryHost


class RecordingRunner:
    def __init__(self, outputs: list[ChildResult]) -> None:
        self.outputs = outputs
        self.argv: list[tuple[str, ...]] = []

    def run_child(self, argv, **_kwargs):
        self.argv.append(tuple(argv))
        return self.outputs.pop(0)


class SmokeRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / "Agents").mkdir()
        self.saved = {
            name: os.environ.get(name)
            for name in ("AGENTS_LIVE_REPO", "XDG_CONFIG_HOME", "XDG_STATE_HOME")
        }
        os.environ["AGENTS_LIVE_REPO"] = str(self.root)
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "config")
        os.environ["XDG_STATE_HOME"] = str(self.root / "state")
        paths.clear_cache()

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        paths.clear_cache()
        self.temporary.cleanup()

    def write_flat_skill(self) -> Path:
        (self.root / ".agents-live.toml").write_text(
            'agent_directories = ["foo"]\n', encoding="utf-8")
        directory = self.root / "foo"
        directory.mkdir()
        prompt = directory / "verify-links.md"
        prompt.write_text(
            "---\n"
            "name: verify-links\n"
            "description: Verify repository links after documentation changes.\n"
            "metadata:\n"
            '  agents-live.schema-version: "1"\n'
            '  agents-live.selector: "fake/echo"\n'
            '  agents-live.schedule: "0 8 * * *"\n'
            '  agents-live.watch: "docs/** debounce 1s"\n'
            "---\n"
            "Verify every changed link.\n",
            encoding="utf-8",
        )
        return prompt


class TestSixRuntimeSmoke(SmokeRepository):
    def test_configured_flat_skill_runs_start_to_stop(self) -> None:
        prompt = self.write_flat_skill()
        spec = agent.load("verify-links", root=self.root)
        self.assertEqual(prompt, spec.prompt_path)
        self.assertRegex(spec.identifier, r"^verify-links-[0-9a-f]{10}$")

        host = MemoryHost()
        registry = {"repos": {"smoke": str(self.root)}, "default_repo": "smoke"}
        with (
            mock.patch.object(lifecycle.repos, "load", return_value=registry),
            mock.patch.object(lifecycle.runtime, "current", return_value=host),
            mock.patch("agents_live.runtime.convergence.current", return_value=host),
        ):
            started = lifecycle.converge(
                additions={self.root: {spec.identifier}})
            self.assertFalse(started.failed)
            self.assertTrue(state.is_started(self.root, spec.identifier))
            self.assertEqual(3, len(host.trigger_store.installed))
            self.assertEqual(1, len(host.supervisor.owned("watcher")))

            rows = status._rows(self.root)
            self.assertEqual("verify-links", rows[0]["name"])
            self.assertEqual(spec.identifier, rows[0]["identifier"])
            self.assertEqual("started", rows[0]["state"])

            runner = RecordingRunner([
                ChildResult(("fake",), 0, '{"text":"links verified"}', ""),
            ])
            outcome = dispatch(
                Firing(spec.identifier, str(self.root), "manual"), runner=runner)
            self.assertTrue(outcome.ok, outcome)
            self.assertEqual("links verified", outcome.text)

            stopped = lifecycle.converge(
                removals={self.root: {spec.identifier}})
            self.assertFalse(stopped.failed)
            self.assertFalse(state.is_started(self.root, spec.identifier))
            self.assertEqual(1, len(host.trigger_store.installed))
            self.assertEqual([], host.supervisor.owned("watcher"))

        watch = parse_watch(spec.execution.watch)
        self.assertTrue(watch.matches("docs/guide.md"))
        self.assertFalse(watch.matches("src/main.py"))

    def test_cli_help_uses_the_packaged_entry_point(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
            mock.patch("agents_live.cli.update_check.interactive", return_value=True),
            mock.patch(
                "agents_live.cli.update_check.consume_notice",
                return_value="new release available",
            ),
            mock.patch("agents_live.cli.update_check.launch_if_stale") as launch,
        ):
            code = cli_main(["--help"])
        self.assertEqual(0, code)
        self.assertIn("agents-live", output.getvalue())
        self.assertIn("start", output.getvalue())
        self.assertIn("new release available", error.getvalue())
        launch.assert_called_once_with()

    def test_json_help_does_not_check_for_updates(self) -> None:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch("agents_live.cli.update_check.interactive") as interactive,
        ):
            self.assertEqual(0, cli_main(["--json", "--help"]))
        interactive.assert_not_called()

    def test_shipped_templates_load_without_external_dependencies(self) -> None:
        templates = Path(agent.__file__).resolve().parents[1] / "skill" / "templates"
        skills = sorted(path.parent for path in templates.glob("*/SKILL.md"))
        self.assertTrue(skills)
        for skill in skills:
            with self.subTest(skill=skill.name):
                spec = agent.load(str(skill), root=templates)
                self.assertEqual(skill.name, spec.name)
                self.assertTrue(spec.properties.description)
                self.assertTrue(spec.body)

    def test_init_installs_the_vendored_skill_payload(self) -> None:
        self.assertEqual("installed", init.install_skill(self.root))
        installed = self.root / ".claude" / "skills" / "agents-live"
        for relative in ("SKILL.md", "VERSION", "docs", "templates"):
            self.assertTrue((installed / relative).exists(), relative)

    def test_administrative_events_use_the_observability_schema(self) -> None:
        obs.admin.record(
            "smoke-admin", root=str(self.root), changed=True,
            correlation_id="operation-1", exit_code=7,
            transcript="transcript.log")
        records = obs.load((obs.admin.log_path(),))
        self.assertEqual(1, len(records))
        self.assertEqual("admin", records[0]["phase"])
        self.assertEqual("smoke-admin", records[0]["operation"])
        self.assertTrue(records[0]["changed"])
        self.assertEqual("operation-1", records[0]["run_id"])
        self.assertEqual(7, records[0]["exit_code"])
        self.assertEqual("transcript.log", records[0]["transcript"])


if __name__ == "__main__":
    unittest.main()
