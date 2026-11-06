"""llama-server for one analysis (#43), with a fake process: nothing is started."""
import tempfile
import unittest
from pathlib import Path

from norev.processor.llama_server import LlamaServerError, LlamaServerSettings, llama_server


class FakeProcess:
    def __init__(self, exits: bool = False) -> None:
        self.exits = exits
        self.terminated = False

    def poll(self):
        return 1 if self.exits else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


class LlamaServerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "llama-server").write_text("")
        (root / "Qwen3-8B-Q4_K_M.gguf").write_text("")
        self.settings = LlamaServerSettings.from_environment({
            "LLAMA_SERVER": str(root / "llama-server"), "LLAMACPP_MODEL": str(root / "Qwen3-8B-Q4_K_M.gguf"),
            "NOREV_LLAMA_LOG": str(root / "log" / "llama.log"), "LLM_PARALLEL": "2"})
        self.started = []

    def popen(self, process):
        def start(cmd, **kwargs):
            self.started.append(cmd)
            return process
        return start

    def test_same_options_as_norev_sh(self):
        cmd = self.settings.command()
        self.assertEqual(cmd[cmd.index("-c") + 1], str(4096 * 2))
        self.assertEqual(cmd[cmd.index("--parallel") + 1], "2")
        self.assertEqual(cmd[cmd.index("--alias") + 1], "Qwen3-8B-Q4_K_M")
        self.assertIn("--reasoning", cmd)

    def test_starts_waits_for_health_and_stops_after_the_block(self):
        process = FakeProcess()
        answers = iter([False, False, True])
        with llama_server(self.settings, is_healthy=lambda url: next(answers), popen=self.popen(process),
                          sleep=lambda s: None) as model:
            self.assertEqual(model, "Qwen3-8B-Q4_K_M")
            self.assertFalse(process.terminated)
        self.assertTrue(process.terminated)

    def test_stops_also_when_the_analysis_fails(self):
        process = FakeProcess()
        answers = iter([False, True])
        with self.assertRaises(ZeroDivisionError):
            with llama_server(self.settings, is_healthy=lambda url: next(answers), popen=self.popen(process), sleep=lambda s: None):
                1 / 0
        self.assertTrue(process.terminated)

    def test_a_running_server_is_used_and_left_running(self):
        with llama_server(self.settings, is_healthy=lambda url: True, popen=self.popen(FakeProcess())):
            pass
        self.assertEqual(self.started, [])

    def test_missing_binary_or_exit_during_startup_is_an_error(self):
        with self.assertRaises(LlamaServerError):
            with llama_server(LlamaServerSettings.from_environment({"LLAMA_SERVER": "/nowhere/llama-server"}),
                              is_healthy=lambda url: False):
                pass
        process = FakeProcess(exits=True)
        with self.assertRaises(LlamaServerError):
            with llama_server(self.settings, is_healthy=lambda url: False, popen=self.popen(process), sleep=lambda s: None):
                pass
        self.assertTrue(process.terminated)


if __name__ == "__main__":
    unittest.main()
