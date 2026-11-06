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
