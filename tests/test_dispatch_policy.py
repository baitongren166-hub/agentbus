import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import agentbus
import config
import logins


class DispatchPolicyTest(unittest.TestCase):
    agent = {"id": "codebuddy", "name": "WorkBuddy", "bin": "codebuddy", "run": "codebuddy", "billing": "none"}
    auth_error = "Authentication required. Please use /login command to sign in to your account"

    def test_zero_exit_with_authentication_error_needs_cli_login(self):
        response = subprocess.CompletedProcess(["codebuddy"], 0, "", self.auth_error)
        with self.assertRaises(agentbus.DispatchBlocked) as failure:
            agentbus.cli_result(response)
        self.assertEqual(failure.exception.code, "cli_login")
        action = logins.dispatch_action(self.agent, failure.exception.code)
        self.assertEqual(action, {"kind": "login", "label": "登录 CodeBuddy CLI"})
        self.assertEqual(logins.method(self.agent)["cmd"], ["codebuddy"])

    def test_workbuddy_prefers_cli_and_desktop_is_only_an_unavailable_action(self):
        with patch("pf.app_exists", return_value=True), patch("pf.which", return_value="/tmp/codebuddy"):
            self.assertEqual(config.dispatch_backend(self.agent), "cli")
            self.assertIsNone(logins.dispatch_action(self.agent, "failed"))
            self.assertEqual(logins.dispatch_action(self.agent, "cli_login")["kind"], "login")
        with patch("pf.app_exists", return_value=True), patch("pf.which", return_value=None):
            self.assertIsNone(config.dispatch_backend(self.agent))
            self.assertEqual(logins.dispatch_action(self.agent, "cli_unavailable")["kind"], "desktop")

    def test_legacy_reply_still_offers_cli_login(self):
        reply = "（WorkBuddy 没有回复，退出码 0）\n" + self.auth_error
        self.assertEqual(logins.dispatch_action(self.agent, "failed", reply)["kind"], "login")

    def test_successful_reply_about_authentication_is_not_a_login_failure(self):
        response = subprocess.CompletedProcess(["codebuddy"], 0, self.auth_error, "")
        self.assertIs(agentbus.cli_result(response), response)
