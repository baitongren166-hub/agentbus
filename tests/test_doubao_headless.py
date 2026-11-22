import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import agentbus
import jobs


class DoubaoHeadlessTest(unittest.TestCase):
    sid, turn = "10001", "20002"

    def run_task(self, results, session=None):
        agent = {"id": "doubao", "name": "豆包", "run": "doubao"}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(agentbus, "doubao_cli_cmd", return_value=["doubao", "headless"]), \
                patch.object(agentbus, "load_config", return_value={}), \
                patch.object(agentbus, "_CUR", {}), \
                patch.object(agentbus, "_run", side_effect=results) as run:
            root = Path(folder)
            result = agentbus.run_agent("doubao", agent, root, "--literal", session, root / "run")
            return result, [call.args[1] for call in run.call_args_list]

    def response(self, **fields):
        result = {"platform": "headless", "conversationId": self.sid, "runId": self.turn, **fields}
        return subprocess.CompletedProcess([], 0, json.dumps(result, indent=2), "")

    def test_creates_and_waits_for_the_accepted_turn(self):
        (reply, sid, log, _), calls = self.run_task([
            self.response(accepted=True, status="running"),
            self.response(status="completed", reply={"text": "确认"}),
        ])
        self.assertEqual((reply, sid), ("确认", self.sid))
        self.assertEqual(calls[0][:4], ["doubao", "headless", "sessions", "create"])
        self.assertEqual(calls[0][-2:], ["--", "--literal"])
        self.assertEqual(calls[1][:7], ["doubao", "headless", "sessions", "wait", self.sid, "--run", self.turn])
        events = []
        jobs._doubao_events(log.splitlines(), events, 1)
        self.assertEqual([e["text"] for e in events], ["豆包已接单", "确认"])

    def test_submits_follow_up_in_same_session_and_does_not_resend_after_timeout(self):
        (reply, sid, log, _), calls = self.run_task([
            self.response(accepted=True, status="running"), agentbus.Stalled("timeout"),
        ], session=self.sid)
        self.assertEqual(calls[0][:5], ["doubao", "headless", "sessions", "send", self.sid])
        self.assertEqual(len(calls), 2)
        self.assertTrue(agentbus._looks_failed(reply))
        self.assertIn(self.turn, reply)
        self.assertIn(self.turn, log)
        self.assertEqual(sid, self.sid)

    def test_login_failure_points_to_the_headless_login_action(self):
        proc = subprocess.CompletedProcess([], 1, "", 'doubao: Doubao login is required; run "doubao headless login"')
        (reply, _, _, _), calls = self.run_task([proc])
        self.assertEqual(len(calls), 1)
        self.assertTrue(agentbus._looks_failed(reply))
        self.assertIn("登录豆包 Headless", reply)

    def test_does_not_accept_a_reply_from_another_turn(self):
        (reply, _, _, _), _ = self.run_task([
            self.response(accepted=True, status="running"),
            self.response(runId="older-run", status="completed", reply={"text": "旧回复"}),
        ])
        self.assertTrue(agentbus._looks_failed(reply))
        self.assertNotIn("旧回复", reply)
