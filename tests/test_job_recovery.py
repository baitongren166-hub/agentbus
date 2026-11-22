import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import agentbus
import brains
import config
import jobs
import logins
import mcp_server
import service


class JobRecoveryTest(unittest.TestCase):
    def test_claude_is_blocked_before_launch_even_with_old_settings(self):
        cfg = {"settings": {"claude_seal": False}}
        agent = {"id": "copy-1", "clone_of": "claude", "run": "claude", "name": "Copy"}
        with patch.object(subprocess, "Popen") as spawn, patch.object(brains, "_detect") as detect:
            self.assertTrue(config.sealed(agent, cfg))
            self.assertFalse(brains.options(agent, fresh=True)["switch"])
            self.assertIsNone(logins.method(agent))
            self.assertIsNone(jobs.resume_cmd("claude", "old-session"))
            for how in ("app", "cli"):
                with self.assertRaisesRegex(ValueError, config.CLAUDE_SEALED):
                    service.open_agent("claude", how)
            for operation in (lambda: service.connect_lead("claude"), lambda: logins.start(agent), lambda: service.start_install("claude"),
                              lambda: mcp_server.start_job({"agent": "Claude", "task": "test"})):
                with self.assertRaisesRegex(ValueError, config.CLAUDE_SEALED):
                    operation()
            with self.assertRaises(agentbus.DispatchBlocked):
                agentbus.run_agent("copy-1", agent, Path.cwd(), "test", None, Path("run"))
            with self.assertRaises(agentbus.DispatchBlocked):
                agentbus._run("generic", ["/tmp/claude", "--help"])
            spawn.assert_not_called()
            detect.assert_not_called()

    def test_codex_failure_ignores_previous_answer(self):
        response = subprocess.CompletedProcess([], 1, json.dumps({"type": "turn.failed", "message": "test failure"}), "")
        agent = {"id": "codex", "name": "Codex", "run": "codex", "bin": "codex"}
        with tempfile.TemporaryDirectory() as folder, patch.object(agentbus, "_run", return_value=response), \
                patch.object(agentbus, "load_config", return_value={}), patch.object(agentbus, "_CUR", {}):
            run = Path(folder) / "job"
            run.with_suffix(".last.md").write_text("old answer")
            text, _, _, _ = agentbus.run_agent("codex", agent, Path(folder), "continue", "test-session", run)
            self.assertIn("test failure", text)
            self.assertTrue(agentbus._looks_failed(text))

    def test_live_session_survives_large_output(self):
        with tempfile.TemporaryDirectory() as folder:
            live = Path(folder) / "job.live"
            live.write_text(json.dumps({"type": "thread.started", "thread_id": "real-session"}) + "\n" + "{}\n" * 100_000)
            self.assertEqual(jobs.session_of({"out": str(live), "kind": "codex"}), "real-session")

    def test_passive_state_never_discovers_models(self):
        with patch.object(brains, "_mem", {}), patch.object(brains, "_load_disk", return_value={}), \
                patch.object(brains, "_detect") as detect:
            brains.options({"run": "opencode", "bin": "opencode"}, discover=False)
            detect.assert_not_called()

    def test_model_refresh_uses_resolved_executable(self):
        result = subprocess.CompletedProcess([], 0, "example-id\tExample Gemini\n", "")
        with patch("pf.resolve", return_value=["/custom/bin/agy", "models"]), \
                patch.object(subprocess, "run", return_value=result) as run:
            self.assertEqual(brains._agy()["models"][0]["id"], "example-id")
            self.assertEqual(run.call_args.args[0], ["/custom/bin/agy", "models"])

    def test_watchdog_only_counts_the_matching_session_log(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "model-io-subagent-sess_other.jsonl").touch()
            own = root / "model-io-sess_012.jsonl"
            own.touch()
            with patch.dict(agentbus.ACTIVITY_LOGS, {"zcode": str(root / "*.jsonl")}):
                probe = agentbus._own_log_probe("zcode", "sess_012")
                self.assertGreater(probe(), 0)
                self.assertEqual(probe.mine, {str(own)})
                self.assertEqual(agentbus._own_log_probe("zcode", None)(), 0)

    def test_followup_is_not_overwritten_by_concurrent_consumer(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(config, "HOME", Path(folder)):
            running = Path(folder) / "job.json"
            running.write_text('{"id": "job"}')
            row = {"_file": str(running)}
            jobs.add_note(row, "第一条")
            writing, written = threading.Event(), threading.Event()
            real_replace = jobs.os.replace

            def add_second():
                writing.set()
                jobs.add_note(row, "第二条")
                written.set()

            producer = threading.Thread(target=add_second)

            def interleave(source, target):
                producer.start()
                self.assertTrue(writing.wait(2))
                self.assertFalse(written.wait(0.1))
                real_replace(source, target)

            with patch.object(jobs.os, "replace", side_effect=interleave):
                self.assertEqual(jobs.take_notes(running), ["第一条"])
            producer.join(3)
            self.assertTrue(written.is_set())
            self.assertEqual(jobs.take_notes(running), ["第二条"])
            self.assertEqual(jobs.take_notes(running, close_when_empty=True), [])
            with self.assertRaises(ValueError):
                jobs.add_note(row, "结束后的补充")

    def test_interrupted_codebuddy_returns_init_session_for_followup(self):
        event = {"type": "system", "subtype": "init", "session_id": "new-session"}
        response = subprocess.CompletedProcess([], -15, json.dumps(event) + "\n", "")
        agent = {"name": "WorkBuddy", "run": "codebuddy", "bin": "codebuddy"}
        with patch.object(agentbus, "_run", return_value=response), \
                patch.object(agentbus, "load_config", return_value={}), patch.object(agentbus, "_CUR", {}):
            _, session, _, _ = agentbus.run_agent("codebuddy", agent, Path.cwd(), "test", None, Path("run"))
        self.assertEqual(session, "new-session")

    def test_generic_nonzero_exit_is_failure_even_with_stdout(self):
        result = subprocess.CompletedProcess([], 42, "运行进度", "permission denied")
        reply = agentbus._plain_cli_reply({"name": "Fixture"}, result)
        self.assertTrue(agentbus._looks_failed(reply))
        self.assertIn("permission denied", reply)

    def test_project_quests_use_the_requested_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("A", "B"):
                bus = root / name
                bus.mkdir()
                row = {"run": name, "kind": "task", "to": "codebuddy", "text": name}
                (bus / "board.jsonl").write_text(json.dumps(row) + "\n")
            def project(name=None):
                name = name or "A"
                return name, root, root / name, ""
            with patch.object(service, "project", side_effect=project):
                self.assertEqual(service.quests("B")[0]["task"], "B")
