"""One-time setup (launcher/setup.py) with fakes: no network, no pip, no llama.cpp binary is run."""
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from norev.adapters.domains.loader import DOMAINS, load_domain
from norev.launcher import hardware, setup
from norev.launcher.hardware import SMALL, Gpu, Hardware
from norev.launcher.llama import LlamaError, LlamaModels, LlamaServer
from norev.launcher.paths import AppPaths, apply_defaults

IGPU_8GB = Hardware("windows", "x64", 7.8, (Gpu("intel", "Intel(R) UHD Graphics", 0.125),))


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = AppPaths(Path(tmp.name), windows=True)
        self.paths.root.mkdir(parents=True, exist_ok=True)


class RunTest(Case):
    def run_setup(self, runs):
        self.llama_calls = []

        def llama(paths, hw, backend):
            self.llama_calls.append(backend)
            return paths.root / "llama.cpp" / backend / "llama-server.exe"

        with redirect_stdout(io.StringIO()):
            return setup.run(self.paths, detect=lambda: IGPU_8GB, packages=lambda profile, speakers: None,
                             speech=lambda paths: None, gguf=lambda model, folder: folder / model.file,
                             llama=llama, runs=runs)

    def test_saves_the_tier_and_the_llama_server_it_installed(self):
        data = self.run_setup(runs=lambda server: True)
        self.assertEqual(self.llama_calls, ["vulkan"])
        self.assertEqual((data["tier"], data["model"], data["llama_backend"]), ("integrated", SMALL.file, "vulkan"))
        self.assertTrue(data["env"]["LLAMA_SERVER"].endswith("llama-server.exe"))
        self.assertEqual(data["env"]["LLAMACPP_GPU_LAYERS"], "auto")

    def test_a_build_that_does_not_run_here_falls_back_to_the_cpu_build(self):
        data = self.run_setup(runs=lambda server: False)
        self.assertEqual(self.llama_calls, ["vulkan", "cpu"])
        self.assertEqual((data["llama_backend"], data["env"]["LLAMACPP_GPU_LAYERS"]), ("cpu", "0"))

    def test_the_saved_tier_reaches_every_stage_and_the_domain(self):
        self.run_setup(runs=lambda server: True)
        env = {}
        apply_defaults(self.paths, env)
        self.assertEqual((env["WHISPER_LANGUAGES"], env["NOREV_DOMAIN"]), ("ro", "local_council"))
        self.assertEqual(Path(env["NOREV_EXTRACT_MODEL"]).name, SMALL.file)
        with mock.patch.dict(os.environ, {"NOREV_DOMAIN_MODEL": env["NOREV_DOMAIN_MODEL"]}):
            self.assertEqual(load_domain(DOMAINS / "corporate").model, SMALL.file)
        self.assertEqual(load_domain(DOMAINS / "corporate").model, "Qwen3-8B-Q4_K_M.gguf")  # unset: domain.toml

    def test_council_defaults_override_an_old_8b_hardware_profile(self):
        hw = Hardware("windows", "x64", 16, ())
        profile = hardware.plan(hw)
        self.assertEqual(profile.model, hardware.LARGE)
        hardware.save(self.paths.hardware, hw, profile, profile.env(self.paths.models))
        env = {}
        apply_defaults(self.paths, env)
        self.assertEqual(env["NOREV_DOMAIN_MODEL"], SMALL.file)
        self.assertEqual(Path(env["NOREV_EXTRACT_MODEL"]).name, SMALL.file)
        self.assertEqual((env["LLAMACPP_CTX"], env["LLM_PARALLEL"]), ("8192", "1"))


class PackagesTest(unittest.TestCase):
    def test_cpu_tier_installs_sherpa_diarization_and_no_cuda_wheels(self):
        calls = []
        with mock.patch.object(setup, "pip", lambda *a: calls.append(a)), redirect_stdout(io.StringIO()):
            setup.install_packages(hardware.plan(IGPU_8GB), speakers=True, find_spec=lambda name: None)
        self.assertEqual([Path(c[-1]).name for c in calls], ["requirements-diarization.txt"])
        self.assertFalse(any("torch" in " ".join(c) for c in calls))  # sherpa-onnx needs no torch
        self.assertFalse(any("requirements-cuda.txt" in " ".join(c) for c in calls))

    def test_the_diarization_is_sherpa_with_no_speaker_limit(self):
        text = (setup.ROOT / "requirements-diarization.txt").read_text(encoding="utf-8")
        self.assertIn("sherpa-onnx==", text)
        self.assertNotIn("\nnemo", text)

    def test_nvidia_tier_installs_the_cuda_wheels(self):
        calls = []
        hw = Hardware("windows", "x64", 15.6, (Gpu("nvidia", "RTX 4070", 8.0),))
        with mock.patch.object(setup, "pip", lambda *a: calls.append(a)), redirect_stdout(io.StringIO()):
            setup.install_packages(hardware.plan(hw), speakers=False, find_spec=lambda name: None)
        self.assertEqual([Path(c[1]).name for c in calls], ["requirements-cuda.txt"])


class LlamaInstallTest(Case):
    def archive(self, folder: Path, name: str, files: dict[str, bytes]) -> Path:
        path = folder / name
        if name.endswith(".zip"):
            with zipfile.ZipFile(path, "w") as z:
                for n, data in files.items():
                    z.writestr(n, data)
        else:
            with tarfile.open(path, "w:gz") as t:
                for n, data in files.items():
                    info = tarfile.TarInfo(n)
                    info.size = len(data)
                    t.addfile(info, io.BytesIO(data))
        return path

    def fake_assets(self, built: dict[str, Path]):
        return lambda build: {n: (p.as_uri(), hashlib.sha256(p.read_bytes()).hexdigest()) for n, p in built.items()}

    def test_cuda_build_and_its_runtime_end_up_in_one_folder(self):
        src = self.paths.root / "src"
        src.mkdir()
        main, runtime = setup.asset_names("windows", "x64", "cuda", "b1")
        built = {main: self.archive(src, main, {"llama-server.exe": b"exe", "ggml-cuda.dll": b"dll"}),
                 runtime: self.archive(src, runtime, {"cudart64_12.dll": b"rt", "cublas64_12.dll": b"bl"})}
        with redirect_stdout(io.StringIO()):
            server = setup.install_llama(self.paths, Hardware("windows", "x64", 16), "cuda", "b1", self.fake_assets(built))
        self.assertEqual(sorted(p.name for p in server.parent.iterdir()),
                         ["cublas64_12.dll", "cudart64_12.dll", "ggml-cuda.dll", "llama-server.exe"])

    def test_a_nested_linux_archive_is_found(self):
        src = self.paths.root / "src"
        src.mkdir()
        main, _ = setup.asset_names("linux", "x64", "vulkan", "b1")
        built = {main: self.archive(src, main, {"llama-b1/llama-server": b"elf", "llama-b1/libllama.so.0": b"so"})}
        with redirect_stdout(io.StringIO()):
            server = setup.install_llama(self.paths, Hardware("linux", "x64", 16), "vulkan", "b1", self.fake_assets(built))
        self.assertEqual(server.name, "llama-server")
        self.assertTrue((server.parent / "libllama.so.0").is_file())

    def test_a_file_with_the_wrong_hash_is_refused_and_removed(self):
        src = self.paths.root / "a.bin"
        src.write_bytes(b"tampered")
        with redirect_stdout(io.StringIO()), self.assertRaises(setup.SetupError):
            setup.download(src.as_uri(), self.paths.root / "out" / "a.bin", "0" * 64)
        self.assertEqual(list((self.paths.root / "out").iterdir()), [])

    def test_a_zip_entry_outside_the_folder_is_refused(self):
        bad = self.archive(self.paths.root, "bad.zip", {"../evil.exe": b"x"})
        with self.assertRaises(setup.SetupError):
            setup._extract(bad, self.paths.root / "into")

    def test_every_pinned_asset_is_a_name_the_release_uses(self):
        for (system, arch, backend) in setup.LLAMA_ASSETS:
            main, runtime = setup.asset_names(system, arch, backend, "b12249")
            self.assertTrue(main.startswith("llama-b12249-bin-"), main)
            self.assertTrue(runtime is None or runtime.startswith("cudart-llama-"), runtime)


class LlamaCpuRetryTest(unittest.TestCase):
    def test_a_gpu_start_failure_is_retried_on_the_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "m.gguf"
            model.write_bytes(b"g")
            tried = []

            class Server(LlamaServer):
                def __enter__(self):
                    tried.append(self.gpu_layers)
                    if self.gpu_layers != "0":
                        raise LlamaError("vulkan driver failed")
                    return self

            models = LlamaModels(Server(binary=Path(tmp) / "llama-server", model=model, log_file=Path(tmp) / "log"), Path(tmp))
            with self.assertLogs("norev.launcher.llama", "WARNING"):
                models.load(str(model))
            self.assertEqual(tried, ["auto", "0"])


class FirstStartTest(Case):
    def test_the_first_start_runs_setup_before_the_app(self):
        from norev.launcher import __main__ as launcher

        with mock.patch.object(launcher, "setup", return_value=1) as ran, \
                mock.patch.dict(os.environ, {"NOREV_HOME": str(self.paths.root)}):
            self.assertEqual(launcher.main([]), 1)
        ran.assert_called_once()

    def test_setup_is_not_run_again_once_the_profile_exists(self):
        from norev.launcher import __main__ as launcher

        self.paths.hardware.write_text(json.dumps({"format": 1, "env": {}}))
        with mock.patch.object(launcher, "setup") as ran, mock.patch.object(launcher, "doctor", return_value=0), \
                mock.patch.dict(os.environ, {"NOREV_HOME": str(self.paths.root)}):
            self.assertEqual(launcher.main(["doctor"]), 0)
        ran.assert_not_called()


if __name__ == "__main__":
    unittest.main()
