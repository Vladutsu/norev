"""Hardware tier (launcher/hardware.py): every laptop class gets settings it can run, no hardware needed."""
import json
import tempfile
import unittest
from pathlib import Path

from norev.launcher import hardware
from norev.launcher.hardware import LARGE, SMALL, Gpu, Hardware, parse_nvidia_smi, parse_windows_adapters, plan


def laptop(ram, *gpus, system="windows", arch="x64"):
    return Hardware(system=system, arch=arch, ram_gb=ram, gpus=tuple(gpus))


RTX_4070 = Gpu("nvidia", "NVIDIA GeForce RTX 4070 Laptop GPU", 8188 / 1024)  # what nvidia-smi reports
RTX_3050_4GB = Gpu("nvidia", "NVIDIA GeForce RTX 3050 Laptop GPU", 4096 / 1024)
RTX_3060_6GB = Gpu("nvidia", "NVIDIA GeForce RTX 3060 Laptop GPU", 6144 / 1024)
IRIS = Gpu("intel", "Intel(R) Iris(R) Xe Graphics", 0.125)
RADEON_APU = Gpu("amd", "AMD Radeon(TM) Graphics", 0.5)
RX_6500M = Gpu("amd", "AMD Radeon RX 6500M", 4.0)


class PlanTest(unittest.TestCase):
    def test_the_reference_machine_keeps_the_reference_setup(self):
        p = plan(laptop(15.6, IRIS, RTX_4070))
        self.assertEqual((p.tier, p.model, p.llama_backend, p.parallel), ("gpu", LARGE, "cuda", 4))
        self.assertEqual((p.whisper_device, p.whisper_compute_type, p.diarization_device), ("cuda", "float16", "cuda"))
        self.assertTrue(p.cuda_wheels)
        self.assertEqual(p.reasons, ())

    def test_16_gb_with_integrated_graphics_keeps_the_8b_model(self):
        p = plan(laptop(15.3, IRIS))
        self.assertEqual((p.tier, p.model, p.llama_backend, p.gpu_layers), ("integrated", LARGE, "vulkan", "auto"))
        self.assertEqual((p.whisper_device, p.whisper_compute_type, p.diarization_device), ("cpu", "int8", "cpu"))
        self.assertFalse(p.cuda_wheels)

    def test_8_gb_with_integrated_graphics_gets_the_4b_model_and_says_why(self):
        p = plan(laptop(7.8, RADEON_APU))
        self.assertEqual((p.tier, p.model, p.parallel), ("integrated", SMALL, 1))
        self.assertTrue(any("Qwen3-4B" in r for r in p.reasons))

    def test_8_gb_with_an_entry_nvidia_gpu_keeps_the_8b_model_split_across_gpu_and_ram(self):
        p = plan(laptop(7.8, IRIS, RTX_3060_6GB))
        self.assertEqual((p.tier, p.model, p.gpu_layers, p.parallel), ("gpu-small", LARGE, "auto", 1))
        self.assertEqual((p.whisper_device, p.whisper_compute_type), ("cuda", "float16"))

    def test_a_4_gb_nvidia_gpu_runs_whisper_in_int8_float16(self):
        p = plan(laptop(15.6, RTX_3050_4GB))
        self.assertEqual((p.tier, p.model), ("gpu-small", LARGE))
        self.assertEqual((p.whisper_device, p.whisper_compute_type), ("cuda", "int8_float16"))

    def test_8_gb_and_a_4_gb_nvidia_gpu_is_not_enough_for_the_8b_model(self):
        self.assertEqual(plan(laptop(7.8, RTX_3050_4GB)).model, SMALL)

    def test_a_dedicated_amd_gpu_uses_vulkan_and_whisper_on_the_cpu(self):
        p = plan(laptop(15.6, RADEON_APU, RX_6500M))
        self.assertEqual((p.tier, p.llama_backend, p.whisper_device), ("gpu-small", "vulkan", "cpu"))

    def test_no_gpu_at_all_runs_everything_on_the_cpu(self):
        p = plan(laptop(15.6, system="linux"))
        self.assertEqual((p.tier, p.llama_backend, p.gpu_layers, p.whisper_device), ("cpu", "cpu", "0", "cpu"))

    def test_apple_silicon_uses_metal(self):
        p = plan(laptop(16.0, Gpu("apple", "Apple Silicon", 0.0), system="darwin", arch="arm64"))
        self.assertEqual((p.tier, p.llama_backend, p.model), ("integrated", "metal", LARGE))

    def test_every_tier_keeps_whisper_large_v3_turbo_and_beam_5(self):
        for hw in (laptop(15.6, RTX_4070), laptop(7.8, IRIS), laptop(7.8), laptop(15.6, RX_6500M)):
            env = plan(hw).env(Path("/m"))
            self.assertEqual((env["WHISPER_MODEL"], env["WHISPER_BEAM_SIZE"]), ("large-v3-turbo", "5"))

    def test_every_stage_uses_the_tier_model_so_it_loads_once(self):
        env = plan(laptop(7.8, IRIS)).env(Path("/m"))
        self.assertEqual({env["NOREV_EXTRACT_MODEL"], env["NOREV_TRANSLATE_MODEL"], env["LLAMACPP_MODEL"]},
                         {str(Path("/m") / SMALL.file)})
        self.assertEqual(env["NOREV_DOMAIN_MODEL"], SMALL.file)


class DetectionParsingTest(unittest.TestCase):
    def test_nvidia_smi(self):
        self.assertEqual(parse_nvidia_smi("NVIDIA GeForce RTX 4070 Laptop GPU, 8188\n"),
                         [Gpu("nvidia", "NVIDIA GeForce RTX 4070 Laptop GPU", 8188 / 1024)])
        self.assertEqual(parse_nvidia_smi("No devices were found\n"), [])

    def test_windows_adapters_skip_nvidia_and_mark_integrated(self):
        gpus = parse_windows_adapters("Intel(R) UHD Graphics|134217728\nNVIDIA GeForce RTX 4070|8589934592\n"
                                      "AMD Radeon RX 6500M|4294967296\nMicrosoft Basic Display Adapter|\n")
        self.assertEqual([(g.vendor, g.integrated) for g in gpus], [("intel", True), ("amd", False), ("other", True)])

    def test_nvidia_is_preferred_over_a_bigger_integrated_carve_out(self):
        self.assertEqual(laptop(16, Gpu("amd", "Radeon", 1.9), RTX_3050_4GB).gpu, RTX_3050_4GB)

    def test_detect_never_crashes_on_this_machine(self):
        found = hardware.detect()
        self.assertGreater(found.ram_gb, 0)
        plan(found)


class SavedProfileTest(unittest.TestCase):
    def test_saved_profile_fills_only_what_the_user_did_not_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hardware.json"
            hw = laptop(7.8, IRIS)
            profile = plan(hw)
            hardware.save(path, hw, profile, profile.env(Path(tmp)))
            env = {"LLM_PARALLEL": "3"}
            data = hardware.apply(path, env)
            self.assertEqual(data["tier"], "integrated")
            self.assertEqual(env["LLM_PARALLEL"], "3")
            self.assertEqual(env["WHISPER_DEVICE"], "cpu")

    def test_a_damaged_or_foreign_profile_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hardware.json"
            for content in ("{not json", json.dumps({"format": 2, "env": {"A": "1"}}),
                            json.dumps({"format": 1, "env": {"lower_case": "x", "PATH": 1}})):
                path.write_text(content)
                env = {}
                hardware.apply(path, env)
                self.assertEqual(env, {})


if __name__ == "__main__":
    unittest.main()
