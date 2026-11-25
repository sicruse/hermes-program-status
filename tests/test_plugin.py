"""Tests for hermes-program-status. Standard library only; run with
``python -m unittest discover -s tests`` from the repository root."""
from __future__ import annotations

import importlib.util
import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_plugin(env: dict[str, str]):
    """Import a fresh copy of the plugin with the given environment."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("hermes_program_status_under_test", ROOT / "__init__.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def read_all(fd: int, timeout: float = 0.2) -> bytes:
    data = b""
    while select.select([fd], [], [], timeout)[0]:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    return data


class FakeCtx:
    def __init__(self):
        self.hooks: dict[str, object] = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_plugin({"HERMES_PROGRAM_STATUS_APP": "hermes"})

    def test_payload_shape(self):
        self.assertEqual(self.mod.sequence("working"), b"\x1b]7501;state=working:app=hermes\x07")

    def test_payload_without_app(self):
        self.assertEqual(self.mod.sequence("idle", app=""), b"\x1b]7501;state=idle\x07")

    def test_unknown_state_rejected(self):
        with self.assertRaises(ValueError):
            self.mod.sequence("thinking")

    def test_sanitize_matches_hermes_rule(self):
        self.assertEqual(self.mod._sanitize("/dev/pts/3"), "dev-pts-3")
        self.assertEqual(self.mod._sanitize("/dev/ttys001"), "dev-ttys001")


class RoutingTests(unittest.TestCase):
    """The agent half finds the right terminal through session files and the registry."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.registry = base / "registry"
        (self.home / "terminal-sessions").mkdir(parents=True)
        self.registry.mkdir()
        self.master, self.slave = os.openpty()
        self.tty = os.ttyname(self.slave)
        self.stem = Path(self.tty).as_posix().strip("/").replace("/", "-")
        env = {
            "HERMES_PROGRAM_STATUS_REGISTRY": str(self.registry),
            "HERMES_PROGRAM_STATUS_HOMES": str(self.home),
            "HERMES_PROGRAM_STATUS_APP": "hermes",
        }
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.mod = load_plugin(env)
        # A live front end in another process registered this tty (our parent pid is alive).
        (self.registry / f"{self.stem}.json").write_text(json.dumps({"tty": self.tty, "pid": os.getppid()}))

    def tearDown(self):
        self.env.stop()
        os.close(self.master)
        os.close(self.slave)
        self.tmp.cleanup()

    def crumb(self, session_id: str):
        (self.home / "terminal-sessions" / f"tty-{self.stem}").write_text(json.dumps({"session_id": session_id}))

    def fire(self, hook: str, **kwargs):
        self.mod.HOOKS[hook](**kwargs)
        return read_all(self.master)

    def test_turn_lifecycle(self):
        self.crumb("s1")
        self.assertIn(b"state=working", self.fire("pre_llm_call", session_id="s1"))
        self.assertIn(b"state=done", self.fire("post_llm_call", session_id="s1"))

    def test_background_subagents_keep_the_session_working(self):
        self.crumb("s1")
        self.fire("pre_llm_call", session_id="s1")
        self.assertIn(b"state=working", self.fire("subagent_start", parent_session_id="s1", child_session_id="c1"))
        self.fire("subagent_start", parent_session_id="s1", child_session_id="c2")
        # The parent ends its turn while the children run.
        ended = self.fire("post_llm_call", session_id="s1")
        self.assertIn(b"state=working", ended)
        self.assertNotIn(b"state=done", ended)
        self.assertEqual(self.fire("subagent_stop", parent_session_id="s1", child_session_id="c1"), b"")
        self.assertEqual(self.fire("subagent_stop", parent_session_id="s1", child_session_id="c2"), b"")
        # Their results re-enter as a new parent turn.
        self.assertIn(b"state=working", self.fire("pre_llm_call", session_id="s1"))
        self.assertIn(b"state=done", self.fire("post_llm_call", session_id="s1"))

    # fire() reads for 0.2 s, so the fallback delay must be longer for its report to land later.
    def test_results_turn_cancels_the_fallback(self):
        self.crumb("s1")
        self.mod._SETTLE = 0.5
        self.fire("subagent_start", parent_session_id="s1", child_session_id="c1")
        self.fire("post_llm_call", session_id="s1")
        self.assertEqual(self.fire("subagent_stop", parent_session_id="s1", child_session_id="c1"), b"")
        self.assertIn(b"state=working", self.fire("pre_llm_call", session_id="s1"))
        time.sleep(0.6)
        self.assertEqual(read_all(self.master), b"")

    def test_session_is_done_when_no_results_turn_follows(self):
        self.crumb("s1")
        self.mod._SETTLE = 0.5
        self.fire("subagent_start", parent_session_id="s1", child_session_id="c1")
        self.fire("post_llm_call", session_id="s1")
        self.assertEqual(self.fire("subagent_stop", parent_session_id="s1", child_session_id="c1"), b"")
        time.sleep(0.6)
        self.assertIn(b"state=done", read_all(self.master))

    def test_nested_subagents_are_ignored(self):
        self.crumb("s1")
        self.assertEqual(self.fire("subagent_start", parent_session_id="c1", child_session_id="c1a"), b"")
        self.assertEqual(self.fire("subagent_stop", parent_session_id="c1", child_session_id="c1a"), b"")

    def test_question_blocks_then_resumes(self):
        self.crumb("s1")
        self.assertIn(b"state=blocked", self.fire("on_human_input_request", session_id="s1"))
        self.assertIn(b"state=working", self.fire("on_human_input_resolved", session_id="s1"))

    def test_approval_blocks_unless_smart(self):
        self.crumb("s1")
        self.assertEqual(self.fire("pre_approval_request", session_id="s1", surface="smart"), b"")
        self.assertIn(b"state=blocked", self.fire("pre_approval_request", session_id="s1", surface="cli"))

    def test_failed_and_interrupted_runs(self):
        self.crumb("s1")
        self.assertIn(b"state=error", self.fire("on_session_end", session_id="s1", failed=True))
        self.assertIn(b"state=idle", self.fire("on_session_end", session_id="s1", interrupted=True))
        self.assertEqual(self.fire("on_session_end", session_id="s1", completed=True), b"")

    def test_other_sessions_are_ignored(self):
        self.crumb("s1")
        self.assertEqual(self.fire("pre_llm_call", session_id="telegram-session"), b"")
        self.assertEqual(self.fire("pre_llm_call"), b"")

    def test_dead_front_end_is_ignored(self):
        self.crumb("s1")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (self.registry / f"{self.stem}.json").write_text(json.dumps({"tty": self.tty, "pid": dead.pid}))
        self.assertEqual(self.fire("pre_llm_call", session_id="s1"), b"")

    def test_same_process_agent_needs_no_session_file(self):
        (self.registry / f"{self.stem}.json").write_text(json.dumps({"tty": self.tty, "pid": os.getpid()}))
        self.assertIn(b"state=working", self.fire("pre_llm_call", session_id="anything"))


class FrontEndTests(unittest.TestCase):
    """The front end registers only inside terminals that consume OSC 7501."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self.tmp.name) / "registry"

    def tearDown(self):
        self.tmp.cleanup()

    def run_frontend(self, extra_env: dict[str, str]) -> tuple[bytes, list[str]]:
        """Load the plugin in a child process whose stdout is a fresh pty."""
        master, slave = os.openpty()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HC_", "HERDR_", "HERMES_PROGRAM_STATUS_"))}
        env.update({"HERMES_PROGRAM_STATUS_REGISTRY": str(self.registry), **extra_env})
        script = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('p', {str(ROOT / '__init__.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "class C:\n    def register_hook(self, *a): pass\n"
            "m.register(C())\n"
            "import json, os; print(json.dumps(sorted(os.listdir(sys.argv[1])) if os.path.isdir(sys.argv[1]) else []), file=sys.stderr)\n"
        )
        proc = subprocess.run([sys.executable, "-c", script, str(self.registry)], stdout=slave,
                              stderr=subprocess.PIPE, env=env, timeout=30)
        output = read_all(master)  # read before closing the slave: macOS drops unread output on close
        os.close(slave)
        os.close(master)
        listed = json.loads(proc.stderr.decode().strip().splitlines()[-1])
        return output, listed

    def test_herdr_core_terminal_registers_and_reports_idle(self):
        output, listed = self.run_frontend({"HC_TERMINAL_ID": "t1"})
        self.assertIn(b"state=idle", output)
        self.assertEqual(len(listed), 1)
        self.assertEqual(list(self.registry.glob("*.json")), [], "registry entry is removed on exit")

    def test_plain_terminal_does_nothing(self):
        output, listed = self.run_frontend({})
        self.assertEqual(output, b"")
        self.assertEqual(listed, [])

    def test_herdr_tui_pane_does_nothing(self):
        output, listed = self.run_frontend({"HC_TERMINAL_ID": "t1", "HERDR_PANE_ID": "w1:p1"})
        self.assertEqual(output, b"")
        self.assertEqual(listed, [])

    def test_any_terminal_opt_in(self):
        output, _ = self.run_frontend({"HERMES_PROGRAM_STATUS_TERMINALS": "any"})
        self.assertIn(b"state=idle", output)


class RegisterTests(unittest.TestCase):
    def test_registers_exactly_the_declared_hooks(self):
        mod = load_plugin({})
        ctx = FakeCtx()
        mod.register(ctx)
        manifest = (ROOT / "plugin.yaml").read_text()
        declared = [line.strip()[2:] for line in manifest.split("provides_hooks:")[1].splitlines() if line.strip().startswith("- ")]
        self.assertEqual(sorted(ctx.hooks), sorted(declared))


if __name__ == "__main__":
    unittest.main()
