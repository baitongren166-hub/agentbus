import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import chat


class ChatFailuresTest(unittest.TestCase):
    agent = {"id": "codebuddy", "name": "WorkBuddy", "run": "codebuddy", "billing": "none"}

    def test_login_error_shows_native_language_and_login_action(self):
        issue = chat.failure_issue(self.agent, "failed", 'Error: {"message":"Authentication required"}')
        self.assertEqual(issue["status"], "cli_login")
        self.assertIn("命令行需要登录", issue["text"])
        self.assertNotIn("Authentication required", issue["text"])
        self.assertEqual(issue["action"]["kind"], "login")
        self.assertEqual(issue["action"]["agent"], "codebuddy")

    def test_channel_limit_returns_an_issue_and_no_job(self):
        issues = []
        with tempfile.TemporaryDirectory() as folder, patch.object(chat, "CHAT", Path(folder)), \
                patch.object(chat, "running", return_value=[{"agent": "one"}]), \
                patch.object(chat, "dispatch") as dispatch:
            jobs = chat._dispatch_all("大厅", ["codebuddy"], {"codebuddy": self.agent}, "task",
                                      "user", 0, None, None, {"max_running": 1}, issues)
            dispatch.assert_not_called()
            self.assertEqual(jobs, [])
            self.assertEqual(issues[0]["status"], "channel_busy")
            self.assertEqual(chat.read("大厅")[0]["issue"], issues[0])

    def test_sealed_chat_target_can_only_offer_another_team_member(self):
        issues = []
        agent = {"id": "claude", "name": "Claude", "run": "claude"}
        with patch.object(chat, "running", return_value=[]), patch.object(chat, "append"), \
                patch.object(chat, "dispatch") as dispatch:
            jobs = chat._dispatch_all("大厅", ["claude"], {"claude": agent}, "task",
                                      "user", 0, None, None, {"max_running": 3}, issues)
        dispatch.assert_not_called()
        self.assertEqual(jobs, [])
        self.assertEqual(issues[0]["action"]["page"], "team")

    def test_legacy_failures_are_translated_without_touching_normal_code_replies(self):
        messages = [
            {"frm": "codebuddy", "text": "❌ （工作失败）\nECONNREFUSED"},
            {"frm": "codebuddy", "text": '示例代码：{"error": "not_found"}'},
        ]
        with patch.object(chat, "agents_dict", return_value={"codebuddy": self.agent}):
            result = chat.display_messages(messages)
        self.assertEqual(result[0]["issue"]["status"], "network")
        self.assertNotIn("ECONNREFUSED", result[0]["text"])
        self.assertEqual(result[1], messages[1])
        self.assertIn("ECONNREFUSED", messages[0]["text"])
