"""Windows launcher (#48): paths, first-start check, llama-server lifecycle, scripts. No GPU, model or network."""
import io
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from norev.adapters.store.encrypted import StoreSettings, create_key_file
from norev.launcher import __main__ as launcher
from norev.launcher.doctor import Check, Probes, ready, report, run_checks
from norev.launcher.llama import LlamaError, LlamaModels, LlamaServer, ModelMissing
from norev.launcher.keys import create_protected_key
from norev.launcher.paths import AppPaths, apply_defaults, cuda_dll_dirs
from norev.testing.fakes import fake_dpapi

ROOT = Path(__file__).resolve().parent.parent


class PathsTest(unittest.TestCase):
    def test_windows_paths_are_in_the_profile_not_the_app_folder(self):
        paths = AppPaths.from_environment({"LOCALAPPDATA": r"C:\Users\ana\AppData\Local"}, windows=True)
        root = Path(r"C:\Users\ana\AppData\Local") / "norev"
        self.assertEqual(paths.root, root)
        self.assertEqual(paths.key, root / "keys" / "norev.key.dpapi")
        self.assertEqual(paths.plain_key, root / "keys" / "norev.key")
        self.assertEqual(paths.llama_server, root / "llama.cpp" / "llama-server.exe")
        self.assertEqual(paths.hf_home, root / "models" / "hf")
        self.assertNotEqual(paths.key.parent, paths.store.parent)
        for path in (paths.store, paths.key, paths.models, paths.logs):
            self.assertNotIn(str(ROOT), str(path))

    def test_store_default_is_the_same_place(self):
        env = {"LOCALAPPDATA": "/tmp/local"}
        self.assertEqual(AppPaths.from_environment(env).store, StoreSettings.from_environment(env).path)

    def test_norev_home_moves_everything_and_linux_has_a_fallback(self):
        self.assertEqual(AppPaths.from_environment({"NOREV_HOME": "/srv/n", "LOCALAPPDATA": "x"}).root, Path("/srv/n"))
        self.assertEqual(AppPaths.from_environment({"XDG_DATA_HOME": "/xdg"}, windows=False).llama_server,
                         Path("/xdg/norev/llama.cpp/llama-server"))

    def test_defaults_never_override_and_the_key_is_set_only_when_it_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp), windows=True)
            env = {"LLAMA_SERVER": "D:/llama/llama-server.exe"}
            apply_defaults(paths, env)
            self.assertEqual(env["LLAMA_SERVER"], "D:/llama/llama-server.exe")
            self.assertEqual((env["HF_HUB_OFFLINE"], env["HF_HOME"]), ("1", str(paths.hf_home)))
            self.assertNotIn("NOREV_STORE_KEY_FILE", env)
            create_key_file(paths.plain_key)
            env = {}
            apply_defaults(paths, env, offline=False)
            self.assertEqual(env["NOREV_STORE_KEY_FILE"], str(paths.plain_key))
            with fake_dpapi():
                create_protected_key(paths.key)
            env = {}
            apply_defaults(paths, env, offline=False)
            self.assertEqual(env["NOREV_STORE_KEY_FILE"], str(paths.key))
            self.assertNotIn("HF_HUB_OFFLINE", env)

    def test_cuda_dll_dirs_are_the_nvidia_wheel_bin_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            for lib in ("cublas", "cudnn"):
                (site / "nvidia" / lib / "bin").mkdir(parents=True)
            (site / "nvidia" / "cuda_runtime" / "include").mkdir(parents=True)
            self.assertEqual([p.parent.name for p in cuda_dll_dirs([site, Path(tmp) / "none"])], ["cublas", "cudnn"])


def fake_probes(**changes) -> Probes:
    values = dict(find_spec=lambda name: object(), which=lambda name: None, cuda_devices=lambda: 1,
                  whisper_cached=lambda name, hf: hf / "whisper", hf_cached=lambda repo, hf: hf / "diar",
                  version=(3, 12), in_venv=True)
    values.update(changes)
    return Probes(**values)


class DoctorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = AppPaths(Path(tmp.name), windows=True)
        dpapi = fake_dpapi()
        dpapi.__enter__()
        self.addCleanup(dpapi.__exit__, None, None, None)

    def install_everything(self):
        self.paths.llama_server.parent.mkdir(parents=True)
        self.paths.llama_server.write_bytes(b"exe")
        self.paths.models.mkdir(parents=True)
        self.paths.gguf.write_bytes(b"gguf")
        create_protected_key(self.paths.key)

    def by_name(self, checks):
        return {c.name: c for c in checks}

    def test_a_ready_machine(self):
        self.install_everything()
        checks = run_checks(self.paths, {}, fake_probes())
        self.assertTrue(ready(checks), report(checks))
        self.assertIn("Totul e pregătit", report(checks))

    def test_an_empty_machine_explains_every_missing_piece(self):
        checks = self.by_name(run_checks(self.paths, {}, fake_probes(
            find_spec=lambda name: None if name in {"webview", "nemo"} else object(),
            whisper_cached=lambda name, hf: None, cuda_devices=lambda: 0)))
        self.assertFalse(ready(list(checks.values())))
        self.assertIn("llama.cpp/releases", checks["llama-server"].fix)
        self.assertIn(str(self.paths.models), checks["Model LLM (GGUF)"].fix)
        self.assertIn("setup-models", checks["Model Whisper"].fix)
        self.assertIn("new-key", checks["Cheia store-ului"].fix)
        self.assertIn("requirements-ui.txt", checks["Fereastra aplicației (pywebview)"].fix)
        self.assertFalse(checks["Placă video (CUDA)"].required)
        self.assertIn("procesor", checks["Placă video (CUDA)"].detail)
        self.assertFalse(checks["Vorbitori (diarizare)"].required)

    def test_a_missing_key_next_to_existing_data_is_not_replaced(self):
        self.paths.store.parent.mkdir(parents=True, exist_ok=True)
        self.paths.store.write_bytes(b"db")
        key = self.by_name(run_checks(self.paths, {}, fake_probes()))["Cheia store-ului"]
        self.assertFalse(key.ok)
        self.assertIn("NU poate citi", key.fix)

    def test_a_key_this_account_cannot_open_is_reported_with_the_backup_fix(self):
        self.paths.key.parent.mkdir(parents=True)
        self.paths.key.write_bytes(b"sealed by another account")
        key = self.by_name(run_checks(self.paths, {}, fake_probes()))["Cheia store-ului"]
        self.assertIn("contul Windows curent nu o poate deschide", key.detail)
        self.assertIn("import-key", key.fix)

    def test_a_plain_key_on_windows_asks_for_protect_key(self):
        create_key_file(self.paths.plain_key)
        checks = self.by_name(run_checks(self.paths, {"NOREV_STORE_KEY_FILE": str(self.paths.plain_key)}, fake_probes()))
        self.assertTrue(checks["Cheia store-ului"].ok)
        self.assertIn("protect-key", checks["Protecția cheii"].fix)
        self.assertFalse(checks["Protecția cheii"].required)

    def test_other_gguf_files_are_suggested(self):
        self.paths.models.mkdir(parents=True)
        (self.paths.models / "gemma-3-4b.gguf").write_bytes(b"x")
        self.assertIn("gemma-3-4b.gguf", self.by_name(run_checks(self.paths, {}, fake_probes()))["Model LLM (GGUF)"].fix)

    def test_a_failing_probe_is_a_finding_not_a_crash(self):
        def boom():
            raise OSError("driver")
        checks = run_checks(self.paths, {}, fake_probes(cuda_devices=boom))
        self.assertIn("gpu", {c.name for c in checks})
        self.assertNotIn("driver", report(checks))

    def test_old_python_and_no_venv(self):
        checks = self.by_name(run_checks(self.paths, {}, fake_probes(version=(3, 10), in_venv=False)))
        self.assertFalse(checks["Python"].ok)
        self.assertFalse(checks["Mediu .venv"].required)

    def test_the_real_probes_open_no_network_connection(self):
        def no_network(*args, **kwargs):
            raise AssertionError("network connection during the first-start check")
        with mock.patch.object(socket, "socket", no_network), mock.patch.object(socket, "create_connection", no_network), \
                mock.patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}):
            checks = run_checks(self.paths, {"HF_HOME": str(self.paths.hf_home)})
        self.assertTrue(checks)
        self.assertTrue(all(isinstance(c, Check) for c in checks))


class FakeProcess:
    def __init__(self, exits_after=None):
        self.polls = 0
        self.exits_after = exits_after
        self.terminated = False

    def poll(self):
        self.polls += 1
        return 1 if self.exits_after is not None and self.polls >= self.exits_after else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class LlamaServerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.binary = self.dir / "llama-server.exe"
        self.binary.write_bytes(b"exe")
        self.model = self.dir / "Qwen3-8B-Q4_K_M.gguf"
        self.model.write_bytes(b"gguf")
        self.started = []

    def server(self, health, process=None, **kw):
        def popen(args, **kwargs):
            self.started.append(args)
            return process or FakeProcess()
        return LlamaServer(self.binary, self.model, self.dir / "logs" / "llama.log", popen=popen, health=health,
                           sleep=lambda s: None, **kw)

    def test_flags_match_norev_sh(self):
        args = self.server(lambda url: True).args()
        self.assertEqual(args[:3], [str(self.binary), "-m", str(self.model)])
        for flag in (["--host", "127.0.0.1"], ["-c", "16384"], ["--parallel", "4"], ["-ngl", "99"],
                     ["-fa", "on"], ["--alias", "Qwen3-8B-Q4_K_M"], ["--reasoning", "off"]):
            self.assertIn(flag, [args[i:i + 2] for i in range(len(args) - 1)])

    def test_starts_waits_for_health_and_stops(self):
        answers = iter([False, False, False, True])
        process = FakeProcess()
        with self.server(lambda url: next(answers), process) as server:
            self.assertEqual(server.url, "http://127.0.0.1:8080")
        self.assertEqual(len(self.started), 1)
        self.assertTrue(process.terminated)

    def test_a_running_server_is_reused_and_left_running(self):
        with self.server(lambda url: True):
            pass
        self.assertEqual(self.started, [])

    def test_missing_pieces_and_a_crash_at_start_are_clear_errors(self):
        self.model.unlink()
        with self.assertRaisesRegex(LlamaError, "modelul GGUF"):
            with self.server(lambda url: False):
                pass
        self.model.write_bytes(b"gguf")
        with self.assertRaisesRegex(LlamaError, "s-a oprit la pornire"):
            with self.server(lambda url: False, FakeProcess(exits_after=2)):
                pass
        with self.assertRaisesRegex(LlamaError, "nu a pornit în 3 s"):
            with self.server(lambda url: False, timeout=3):
                pass


class FakeGpu:
    """llama-server processes on one port: healthy while one runs; records starts and stops."""

    def __init__(self, external=False):
        self.events, self.running, self.external = [], [], external

    def popen(self, args, **kwargs):
        if self.running or self.external:
            raise AssertionError("two models on the GPU")
        name = Path(args[args.index("-m") + 1]).name
        gpu = self

        class Process(FakeProcess):
            def terminate(self):
                gpu.running.remove(name)
                gpu.events.append(("stop", name))
        self.running.append(name)
        self.events.append(("start", name))
        return Process()

    def health(self, url):
        return bool(self.running) or self.external


class LlamaModelsTest(unittest.TestCase):
    """#62: one llama-server per stage's model, restarted only when the model changes."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "llama-server.exe").write_bytes(b"exe")
        self.models_dir = self.dir / "models"
        for path in ("qwen3/Qwen3-8B-Q4_K_M.gguf", "medgemma-4b-it-Q4_K_M.gguf"):
            (self.models_dir / path).parent.mkdir(parents=True, exist_ok=True)
            (self.models_dir / path).write_bytes(b"gguf")
        self.qwen = self.models_dir / "qwen3" / "Qwen3-8B-Q4_K_M.gguf"

    def models(self, gpu):
        template = LlamaServer(self.dir / "llama-server.exe", self.qwen, self.dir / "logs" / "llama.log",
                               popen=gpu.popen, health=gpu.health, sleep=lambda s: None)
        return LlamaModels(template, self.models_dir)

    def test_the_server_restarts_only_when_the_model_changes(self):
        gpu = FakeGpu()
        models = self.models(gpu)
        self.assertEqual(models.load(str(self.qwen)), "Qwen3-8B-Q4_K_M")  # translation, by path
        models.load("Qwen3-8B-Q4_K_M")  # extraction, by alias: the same file, no restart
        self.assertEqual(models.load("medgemma-4b-it-Q4_K_M.gguf"), "medgemma-4b-it-Q4_K_M")  # Domain stage
        models.stop()
        models.stop()  # nothing left to stop
        self.assertEqual(gpu.events, [("start", "Qwen3-8B-Q4_K_M.gguf"), ("stop", "Qwen3-8B-Q4_K_M.gguf"),
                                      ("start", "medgemma-4b-it-Q4_K_M.gguf"), ("stop", "medgemma-4b-it-Q4_K_M.gguf")])
        self.assertEqual(gpu.running, [])

    def test_a_missing_model_is_a_clear_error_and_starts_nothing(self):
        gpu = FakeGpu()
        models = self.models(gpu)
        with self.assertRaisesRegex(ModelMissing, "missing-7b.gguf nu există"):
            models.load("missing-7b.gguf")
        with self.assertRaisesRegex(ModelMissing, "niciun model"):
            models.load(None)
        (self.models_dir / "copy").mkdir()
        (self.models_dir / "copy" / "medgemma-4b-it-Q4_K_M.gguf").write_bytes(b"gguf")
        with self.assertRaisesRegex(ModelMissing, "de mai multe ori"):
            models.load("medgemma-4b-it-Q4_K_M")
        self.assertEqual(gpu.events, [])

    def test_a_server_started_outside_norev_serves_only_its_own_model(self):
        gpu = FakeGpu(external=True)
        models = self.models(gpu)
        models.load("Qwen3-8B-Q4_K_M")
        models.load("Qwen3-8B-Q4_K_M")
        with self.assertRaisesRegex(LlamaError, "pornit din afara norev"):
            models.load("medgemma-4b-it-Q4_K_M")
        models.stop()  # never stops a server norev did not start
        self.assertEqual(gpu.events, [])

    def test_parallel_calls_for_one_model_start_it_once(self):
        gpu = FakeGpu()
        models = self.models(gpu)
        threads = [threading.Thread(target=models.load, args=("Qwen3-8B-Q4_K_M",)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(gpu.events, [("start", "Qwen3-8B-Q4_K_M.gguf")])
        models.stop()


class LauncherTest(unittest.TestCase):
    def test_doctor_command_on_an_empty_profile(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"NOREV_HOME": tmp}), \
                redirect_stdout(io.StringIO()) as out:
            for name in ("NOREV_STORE_KEY_FILE", "LLAMA_SERVER", "NOREV_EXTRACT_MODEL", "LLAMACPP_MODEL",
                         "NOREV_STORE_PATH", "HF_HOME"):
                os.environ.pop(name, None)
            code = launcher.main(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("llama-server", out.getvalue())

    def test_new_key_creates_once_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as out, fake_dpapi():
            paths = AppPaths(Path(tmp), windows=True)
            launcher.new_key(paths)
            first = paths.key.read_bytes()
            launcher.new_key(paths)
            self.assertEqual(paths.key.read_bytes(), first)
        self.assertIn("Nu o suprascriu", out.getvalue())

    def test_process_refuses_without_a_key(self):
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(launcher.process(AppPaths(Path("/nowhere"), windows=True), "a.wav", None), 1)
        self.assertIn("new-key", out.getvalue())


class ScriptsTest(unittest.TestCase):
    def test_windows_scripts_use_the_windows_venv_and_are_checked_out_with_crlf(self):
        self.assertIn("*.cmd text eol=crlf", (ROOT / ".gitattributes").read_text())
        for name in ("install.cmd", "norev.cmd", "check.cmd"):
            self.assertIn(rb".venv\Scripts\python.exe", (ROOT / name).read_bytes(), name)

    def test_install_downloads_no_model(self):
        install = (ROOT / "install.cmd").read_text()
        self.assertNotIn("setup-models", install)
        self.assertNotIn("http", install)

    def test_check_sh_and_check_cmd_run_the_same_python_checks(self):
        self.assertIn("scripts/check.py", (ROOT / "check.sh").read_text())
        self.assertIn(r"scripts\check.py", (ROOT / "check.cmd").read_text())


if __name__ == "__main__":
    unittest.main()
