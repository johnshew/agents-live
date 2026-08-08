from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from agents_live import agent, state, triggers
from agents_live.dispatch import Firing, _RunLock, dispatch
from agents_live.definition_migrate import MigrationError, convert
from agents_live.runtime import (
    ChildResult,
    Health,
    InstalledTrigger,
    ProcessRef,
    Subscription,
    WatchSyntaxError,
    diff,
    parse_schedule,
    parse_watch,
)
from agents_live.runtime.budget import claim as claim_budget
from agents_live.runtime.hosts.processes import LocalChildRunner
from agents_live.runtime.hosts.posix import PosixHost


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

    def test_rejects_flat_and_retired_formats(self) -> None:
        (self.root / "Agents" / "old.md").write_text(
            "---\ndescription: old\nruntime: none\n---\nold\n", encoding="utf-8")
        with self.assertRaisesRegex(agent.DefinitionError, "5.x flat format"):
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


def _rendered(kind: str, key: str, fingerprint: str):
    from agents_live.runtime import RenderedSubscription
    return RenderedSubscription(
        key, "repo:/r", kind, fingerprint, "rendered", ("watch",) if kind == "watch" else ())


class TestStartedState(TempRepository):
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
    def test_ports_do_not_import_each_other_and_cli_stays_on_ports(self) -> None:
        package = Path(__file__).parents[1] / "src" / "agents_live"
        runtime_imports = _imports(package / "runtime")
        agent_imports = _imports(package / "agent")
        self.assertFalse(any(name.startswith("agents_live.agent") for name in runtime_imports))
        self.assertFalse(any(name.startswith("agents_live.runtime") for name in agent_imports))
        allowed = {
            "agents_live.agent", "agents_live.dispatch", "agents_live.obs",
            "agents_live.runtime", "agents_live.state", "agents_live.cli",
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
