import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import agentbus
import brains
import config
import service


class AntigravityModelsTest(unittest.TestCase):
    model = "gemini-4-argon-medium"
    agent = {"id": "gemini", "name": "Antigravity", "run": "agy", "bin": "agy"}

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        folder = stack.enter_context(tempfile.TemporaryDirectory())
        self.home = Path(folder)
        self.cache = self.home / "brains.json"
        stack.enter_context(patch.object(config, "HOME", self.home))
        stack.enter_context(patch.object(config, "CONFIG", self.home / "config.json"))
        stack.enter_context(patch.object(config, "current_project", return_value=""))
        stack.enter_context(patch.object(brains, "CACHE", self.cache))
        stack.enter_context(patch.object(brains, "_mem", {}))
        stack.enter_context(patch.object(brains, "_busy", set()))

    def test_failed_refresh_cannot_replace_cached_catalog(self):
        cached = {"models": [{"id": self.model, "name": "Argon Medium", "efforts": []}],
                  "current": {}, "switch": True, "t": 1}
        self.cache.write_text(json.dumps({"agy": cached}))
        results = [
            subprocess.CompletedProcess([], 1, f"{self.model}\tArgon Medium\n", "not logged in"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.TimeoutExpired(["agy", "models"], 15),
        ]
        for result in results:
            with self.subTest(result=result), patch.object(brains.subprocess, "run", side_effect=[result]):
                brains._detect("agy")
                self.assertEqual(json.loads(self.cache.read_text())["agy"], cached)
                self.assertEqual(brains.options(self.agent, discover=False)["models"], cached["models"])

    def test_successful_refresh_persists_new_catalog(self):
        proc = subprocess.CompletedProcess([], 0, f"{self.model}\tArgon Medium\n", "")
        with patch.object(brains.subprocess, "run", return_value=proc):
            brains._detect("agy")
        brains._mem.clear()
        opts = brains.options(self.agent, discover=False)
        self.assertEqual(opts["models"][0]["id"], self.model)
        self.assertFalse(opts["detecting"])

    def test_empty_legacy_cache_keeps_saved_model_and_manual_entry(self):
        self.cache.write_text(json.dumps({"agy": {"models": [], "current": {}, "switch": True, "t": time.time()}}))
        cfg = {"brains": {"gemini": {"model": self.model}}}
        with patch.object(service, "load_config", return_value=cfg), \
                patch.object(service, "agents_dict", return_value={"gemini": self.agent}), \
                patch.object(brains, "_detect") as detect:
            self.assertTrue(service.brain_brief({}, "gemini", self.agent)["switch"])
            self.assertEqual(service.brain_brief(cfg, "gemini", self.agent)["model"], self.model)
            detect.assert_not_called()
            with patch.object(brains.threading, "Thread"):
                opts = service.brain_options("gemini")
        self.assertTrue(opts["free"])
        self.assertEqual(opts["models"][0]["id"], self.model)

    def test_each_dispatch_forwards_the_latest_saved_model_without_discovery(self):
        with patch.object(agentbus, "_CUR", {}), patch.object(agentbus, "conv_id", return_value="test"), \
                patch.object(brains, "options") as options, patch.object(agentbus, "_run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"event":"result","result":{"response":"ok"}}\n', "")
            for model in (self.model, "gemini-test-high", ""):
                config.save_json(config.CONFIG, {"brains": {"gemini": {"model": model}}})
                agentbus.run_agent("gemini", self.agent, self.home, "测试", None, self.home / "job.jsonl")
                args = run.call_args.args[1]
                if model:
                    self.assertEqual(args[args.index("--model") + 1], model)
                else:
                    self.assertNotIn("--model", args)
            options.assert_not_called()


if __name__ == "__main__":
    unittest.main()
