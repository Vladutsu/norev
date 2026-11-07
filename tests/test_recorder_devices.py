import unittest

from norev.adapters.recorder.recorder import (
    parse_alsa_capture_devices,
    parse_dshow_devices,
    resolve_device,
)


class RecorderDevicesTest(unittest.TestCase):
    def test_dshow_lists_audio_devices_only(self) -> None:
        # FFmpeg log fragments as PyAV captures them, joined
        log = (
            '"Integrated Camera" (video)\n'
            '  Alternative name "@device_pnp_\\\\?\\usb#vid_13d3"\n'
            '"Microphone Array (Realtek(R) Audio)" (audio)\n'
            '  Alternative name "@device_cm_{33D9A762}\\wave_{70630C6C}"\n'
            '"Microfon (USB Headset)" (audio)\n'
        )
        self.assertEqual(
            parse_dshow_devices(log),
            ["Microphone Array (Realtek(R) Audio)", "Microfon (USB Headset)"],
        )

    def test_alsa_lists_capture_pcms_as_plughw(self) -> None:
        pcm = (
            "00-00: ALC3246 Analog : ALC3246 Analog : playback 1 : capture 1\n"
            "00-03: HDMI 0 : HDMI 0 : playback 1\n"
            "01-00: USB Audio : USB Audio : capture 1\n"
        )
        self.assertEqual(parse_alsa_capture_devices(pcm), ["plughw:0,0", "plughw:1,0"])

    def test_device_name_is_used_as_given(self) -> None:
        self.assertEqual(resolve_device("plughw:1,0"), "plughw:1,0")


if __name__ == "__main__":
    unittest.main()


class AlsaDefaultsTest(unittest.TestCase):
    """PyAV's bundled ALSA gets the system's configuration and plugins, so "default" reaches PipeWire."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = self.root / "alsa.conf"
        self.plugins = self.root / "alsa-lib"

    def defaults(self, env):
        from norev.adapters.recorder.recorder import alsa_defaults

        alsa_defaults(env, configs=(self.root / "missing.conf", self.config),
                      plugin_dirs=(self.root / "missing", self.plugins))
        return env

    def test_the_first_existing_config_and_plugin_folder_are_set(self):
        self.config.write_text("")
        self.plugins.mkdir()
        self.assertEqual(self.defaults({}), {"ALSA_CONFIG_PATH": str(self.config), "ALSA_PLUGIN_DIR": str(self.plugins)})

    def test_what_the_user_set_is_kept(self):
        self.config.write_text("")
        self.plugins.mkdir()
        env = self.defaults({"ALSA_CONFIG_PATH": "/mine.conf"})
        self.assertEqual(env["ALSA_CONFIG_PATH"], "/mine.conf")
        self.assertEqual(env["ALSA_PLUGIN_DIR"], str(self.plugins))

    def test_nothing_is_set_without_alsa_files(self):
        self.assertEqual(self.defaults({}), {})
