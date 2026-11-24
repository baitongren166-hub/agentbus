"""Feishu and away-mode regressions using isolated files and mocked services."""
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import agentbus
import away
import chat
import config
import feishu
import mcp_server
import pf
import playbook


class FeishuReviewTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for module, name, value in (
            (config, "HOME", self.root),
            (feishu, "HOME", self.root),
            (feishu, "STATE", self.root / "feishu.json"),
            (feishu, "STATUS", self.root / "feishu-status.json"),
            (chat, "CHAT", self.root / "chat"),
            (away, "STATE", self.root / "away.json"),
        ):
            self.enter_patch(patch.object(module, name, value))

    def enter_patch(self, cm):
        self.addCleanup(cm.stop)
        return cm.start()

    def test_only_one_sender_can_pair_and_non_owner_cannot_download(self):
        feishu.update(lambda st: st.update(enabled=True, pair="125389"))

        def event(sender, text):
            return {"sender": {"sender_type": "user", "sender_id": {"open_id": sender}},
                    "message": {"chat_type": "p2p", "chat_id": sender, "message_type": "text",
                                "content": json.dumps({"text": text})}}

        with patch.object(feishu, "send") as send, patch.object(feishu, "_pick_card"), patch.object(feishu, "_text_files") as download:
            feishu.on_message(event("invalid", "x125389"))
            self.assertFalse(feishu.state().get("owner"))
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda sender: feishu.on_message(event(sender, "125389")), ["owner-a", "owner-b"]))
            self.assertIn(feishu.state()["owner"], ("owner-a", "owner-b"))
            self.assertIsNone(feishu.state()["pair"])
            self.assertEqual(sum(call.args[2].startswith("绑定好了") for call in send.call_args_list if call.args[1] == "text"), 1)
            download.assert_not_called()

    def test_connection_status_ignores_old_credentials_and_disabled_state(self):
        feishu.update(lambda st: st.update(enabled=True, app_id="example", revision="old"))
        feishu._status(ok=True, identity=("example", "old"))
        self.assertTrue(feishu.connection_status()["ok"])
        feishu.update(lambda st: st.update(revision="new"))
        self.assertEqual(feishu.connection_status(), {})
        feishu._status(ok=True, identity=("example", "new"))
        feishu.update(lambda st: st.update(enabled=False))
        self.assertEqual(feishu.connection_status(), {})
        self.assertIsNone(feishu.handle({"header": {"event_id": "disabled"}}))
        self.assertEqual(feishu.state()["seen"], [])

    def test_negative_protobuf_value_roundtrips_without_looping(self):
        frame = {"service": -1, "method": 1, "headers": {"type": "event"}, "payload": b"{}"}
        decoded = feishu.frame_decode(feishu.frame_encode(frame))
        self.assertEqual(decoded["service"], (1 << 64) - 1)
        self.assertEqual(decoded["payload"], b"{}")

    def test_paid_lead_retry_does_not_take_newly_queued_messages(self):
        key, old_job = "thread", "old"
        old = {"kind": "owner", "text": "old task"}
        new = {"kind": "owner", "text": "new task"}

        def setup(st):
            st["enabled"] = True
            st["chats"][key] = {"project": "example", "lead": "codex", "conv": "conv", "briefed": True}
            st["queue"][key] = [new]
            st["lead_retry"][key] = {"job": old_job, "conv": "conv", "items": [old]}
        feishu.update(setup)
        with patch.object(mcp_server, "start_job", return_value="new-job") as start, patch.object(feishu, "reply", return_value="card"),                 patch.object(feishu, "_face"), patch.object(feishu, "_name", side_effect=str), patch.object(feishu, "_root"):
            feishu._retry_lead(key, "unrelated")
            start.assert_not_called()
            feishu._retry_lead(key, old_job)
            feishu._retry_lead(key, old_job)
            start.assert_called_once()
            self.assertEqual(start.call_args.args[0]["task"], "old task")
            self.assertTrue(start.call_args.args[0]["allow_credits"])
        self.assertEqual(feishu.state()["queue"][key], [new])

    def test_chat_credits_takes_original_message_and_consumes_confirmation(self):
        channel = "example"
        source = chat.append(channel, "user", "original")
        failed = chat.append(channel, "agentbus", "quota", source=source["id"], issue={"status": "quota", "agent": "opencode"})
        chat.append(channel, "user", "unrelated")
        value = {"ch": channel, "agent": "opencode", "row": failed["id"]}
        with patch.object(chat, "_dispatch_all", return_value=["retry"]) as dispatch:
            feishu._credits(value)
            feishu._credits(value)
            dispatch.assert_called_once()
            self.assertEqual(dispatch.call_args.args[3], "original")
            self.assertTrue(dispatch.call_args.kwargs["allow_credits"])

    def test_chat_reply_uses_job_not_agent_to_find_pending_card(self):
        feishu.update(lambda st: st["pending"].update({"room|job-a": ["a", 0], "room|job-b": ["b", 0]}))
        with patch.object(feishu, "patch_card") as patch_card:
            key, card = feishu._pending("room", "job-a", "opencode")
            feishu._deliver("thread", key, card, {})
        patch_card.assert_called_once_with("a", {})
        self.assertEqual(feishu.state()["pending"], {"room|job-b": ["b", 0]})

    def test_background_job_does_not_store_full_prompt_or_put_it_in_argv(self):
        with patch.object(mcp_server, "JOBS", self.root / "jobs"), patch.object(mcp_server, "project", return_value=("example", self.root, None, None)),                 patch.object(mcp_server.subprocess, "Popen") as popen:
            def spawn(argv, *, stdin, **kwargs):
                self.assertNotIn("test prompt", argv)
                self.assertEqual(stdin.read(), b"test prompt")
                self.assertEqual(kwargs["env"]["AGENTBUS_CONV"], "example-conv")
                return Mock(pid=987)
            popen.side_effect = spawn
            jid = mcp_server.start_job({"agent": "opencode", "task": "test prompt", "project": "example"},
                                       env_extra={"AGENTBUS_CONV": "example-conv"})
            record = json.loads((mcp_server.JOBS / f"{jid}.json").read_text(encoding="utf-8"))
        self.assertEqual(record["conv"], "example-conv")
        self.assertNotIn("cmd", record)

    def test_headless_window_cannot_replace_desktop_lead(self):
        with patch.dict(os.environ, {"AGENTBUS_FEISHU": "thread", "AGENTBUS_DEPTH": "0"}),                 patch.object(config, "save_json") as save:
            config.touch_lead("codex", "mcp", "example-conv")
        save.assert_not_called()

    def test_setup_task_stays_in_the_selected_window(self):
        with patch.object(agentbus, "LEAD_TASKS", self.root / "lead_tasks.json"):
            agentbus.add_lead_task("setup", agent="codex", conv="selected")
            self.assertIsNone(agentbus.take_lead_task("codex", "other"))
            self.assertEqual(agentbus.take_lead_task("codex", "selected"), "setup")
            self.assertIsNone(agentbus.take_lead_task("codex", "selected"))

    def test_lid_restore_failure_keeps_original_recovery_state(self):
        away.save_json(away.STATE, {"on": True, "lid": True, "lid_changed": True})
        with patch.object(away, "IS_MAC", True), patch.object(away, "_lid", return_value=False),                 patch.object(away, "laptop", return_value=True), patch.object(away, "authorized", return_value=False),                 patch.object(away, "checks", return_value=[]), patch.object(away, "_stop_awake") as stop:
            result = away.set_on(False)
        self.assertTrue(result["on"])
        self.assertTrue(away.state()["lid_changed"])
        stop.assert_not_called()

    def test_malformed_system_proxy_is_reported_without_url_or_credentials(self):
        fixture_marker = "placeholder-password"
        invalid_proxy = "http://" + "@".join((f"name:{fixture_marker}", "localhost:invalid"))
        with patch.object(away, "IS_MAC", False), patch.object(away, "projects", return_value={}):
            with patch("urllib.request.getproxies", return_value={"https": invalid_proxy}):
                checks = away.checks()
        self.assertEqual(checks[0]["name"], "代理")
        self.assertFalse(checks[0]["ok"])
        self.assertNotIn(fixture_marker, str(checks))

    def test_playbook_excludes_scripts_outside_project_and_hidden_configs(self):
        project = self.root / "project"
        project.mkdir()
        (self.root / "outside.py").write_text("outside")
        (project / ".config.json").write_text("{}")
        (project / "inside.py").write_text("inside")
        try:
            (project / "linked.py").symlink_to(self.root / "outside.py")
        except OSError:
            if os.name != "nt":
                raise
        pb = {"steps": [], "workflow": "`../outside.py` `.config.json` `inside.py` `linked.py`", "lessons": []}
        with patch.object(playbook, "_installed_skills", return_value={}), patch.object(playbook, "_installed_mcps", return_value=set()):
            reqs = playbook.scan_requires(pb, project)
        self.assertEqual([r["name"] for r in reqs if r.get("bundled")], ["inside.py"])

    def test_proxy_keeps_local_bypass_when_environment_already_has_a_proxy(self):
        with patch.object(pf, "IS_MAC", False), patch.object(pf, "IS_WIN", False),                 patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.example", "no_proxy": "internal.example"}, clear=True):
            pf.proxy_env()
            self.assertEqual(os.environ["HTTPS_PROXY"], "http://proxy.example")
            self.assertIn("127.0.0.1", os.environ["no_proxy"])
            self.assertIn("internal.example", os.environ["NO_PROXY"])


if __name__ == "__main__":
    unittest.main()
