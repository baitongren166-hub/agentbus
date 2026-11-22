import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import agentbus
import jobs


def jsonl(rows):
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


class CodeBuddyStreamTest(unittest.TestCase):
    events = [
        {"type": "system", "subtype": "init", "session_id": "new-session"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "先查看输入文件"}, {"type": "tool_use", "id": "tool1", "name": "Read"}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tool1", "content": [{"type": "text", "text": "读取完成"}]}]}},
    ]

    def test_returns_live_tool_events_before_the_cli_exits(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "job.live"
            source.write_text(jsonl(self.events) + '{"type":', "utf-8")
            view = jobs.view({"id": "test-job", "kind": "codebuddy", "out": str(source)})
        self.assertTrue(view["streams"])
        self.assertEqual(view["session"], "new-session")
        self.assertEqual([e["k"] for e in view["events"]], ["做", "说", "做", "结果"])
        self.assertEqual(view["events"][-1]["text"], "Read 完成")
        self.assertEqual(view["events"][-1]["detail"], "读取完成")

    def test_run_agent_requests_stream_json_and_uses_terminal_result_for_followups(self):
        terminal = {"type": "result", "subtype": "success", "result": "已完成", "session_id": "next-session"}
        stream = jsonl([*self.events, terminal])
        cmd_result = subprocess.CompletedProcess([], 0, stream, "")
        agent = {"name": "WorkBuddy", "run": "codebuddy", "bin": "codebuddy"}
        with patch.object(agentbus, "_run", return_value=cmd_result) as run, \
                patch.object(agentbus, "load_config", return_value={}), patch.object(agentbus, "_CUR", {}):
            result = agentbus.run_agent("codebuddy", agent, Path.cwd(), "任务", "existing", Path("test"))
        args = run.call_args.args[1]
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertEqual(args[args.index("--resume") + 1], "existing")
        self.assertEqual(result[:3], ("已完成", "next-session", stream))

    def test_legacy_job_uses_only_its_bound_native_session_after_start(self):
        native = [
            {"type": "function_call", "timestamp": 900, "sessionId": "active", "name": "Old"},
            {"type": "function_call", "timestamp": 1200, "sessionId": "other", "name": "Other"},
            {"type": "function_call", "timestamp": 1500, "sessionId": "active", "name": "Read"},
            {"type": "message", "timestamp": 1600, "sessionId": "active", "role": "assistant",
             "content": [{"type": "output_text", "text": "读取中"}]},
        ]
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "active.jsonl"
            source.write_text(jsonl(native), "utf-8")
            events = jobs.events({"kind": "codebuddy", "start": 1, "session": "active", "logs": [str(source)]})
        self.assertEqual([e["text"] for e in events], ["调用工具 Read", "读取中"])

    def test_streaming_authentication_error_keeps_the_cli_login_action(self):
        stdout = jsonl([*self.events, {"type": "result", "is_error": True, "result": "Authentication required"}])
        with self.assertRaises(agentbus.DispatchBlocked) as failure:
            agentbus.cli_result(subprocess.CompletedProcess([], 0, stdout, ""), "codebuddy")
        self.assertEqual(failure.exception.code, "cli_login")

    def test_buffered_array_returns_the_stage_report_and_session(self):
        reply = "已交阶段汇报，剩余任务按主位决定暂停。"
        terminal = {"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": "paused-session"}
        stdout = json.dumps([{"type": "reasoning", "content": []}, terminal], ensure_ascii=False, indent=2)
        cmd_result = subprocess.CompletedProcess([], 0, stdout, "")
        agent = {"name": "WorkBuddy", "run": "codebuddy", "bin": "codebuddy"}
        with patch.object(agentbus, "_run", return_value=cmd_result), \
                patch.object(agentbus, "load_config", return_value={}), patch.object(agentbus, "_CUR", {}):
            result = agentbus.run_agent("codebuddy", agent, Path.cwd(), "任务", None, Path("test"))
        self.assertEqual(result[:3], (reply, "paused-session", stdout))

    def test_saved_array_recovers_the_report_from_a_misclassified_job(self):
        reply = "已交阶段汇报，剩余任务按主位决定暂停。"
        records = [
            {"type": "reasoning", "content": "内部推理", "timestamp": 900},
            {"type": "function_call", "name": "Read", "timestamp": 1000},
            {"type": "function_call_result", "name": "Read", "timestamp": 1100, "status": "completed"},
            {"type": "message", "role": "assistant", "timestamp": 1200,
             "content": [{"type": "output_text", "text": reply}]},
            {"type": "result", "subtype": "success", "is_error": False, "result": reply},
        ]
        with tempfile.TemporaryDirectory() as folder:
            runs = Path(folder) / "runs"
            runs.mkdir()
            source = runs / "test-codebuddy.jsonl"
            source.write_text(json.dumps(records, ensure_ascii=False, indent=2), "utf-8")
            source.with_suffix(".md").write_text("## 日志\n\n## 回复\n\n（CodeBuddy CLI 没有回复，退出码 0）\n", "utf-8")
            history = jobs.last_done("codebuddy", "codebuddy", [folder])
        self.assertEqual(history["reply"], reply)
        self.assertEqual([e["text"] for e in history["events"]], ["调用工具 Read", "Read 完成", reply])

    def test_codebuddy_exit_without_terminal_result_has_a_clear_failure(self):
        stdout = jsonl(self.events)
        cmd_result = subprocess.CompletedProcess([], 0, stdout, "")
        agent = {"name": "WorkBuddy", "run": "codebuddy", "bin": "codebuddy"}
        with patch.object(agentbus, "_run", return_value=cmd_result), \
                patch.object(agentbus, "load_config", return_value={}), patch.object(agentbus, "_CUR", {}):
            reply, sid, raw, _ = agentbus.run_agent("codebuddy", agent, Path.cwd(), "任务", None, Path("test"))
        self.assertIn("提前退出", reply)
        self.assertNotIn('"type"', reply)
        self.assertTrue(agentbus._looks_failed(reply))
        self.assertEqual(raw, stdout)
