"""python -m norev.session (#19): the CLI on the encrypted store, with fakes for every model."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from LLM import pipeline
from norev import session as cli
from norev.adapters.store.encrypted import StoreSettings, create_key_file, open_store
from norev.core.models import Session
from norev.testing.fakes import FakeActionExtractor, FakeLlmClient
from test_usecases_session_flow import ANSWERS, GENERIC, SPOKEN, TRANSLATION, TURNS


class FakeProcessor(FakeLlmClient):
    """Stands in for the Central Processor: Whisper, diarization and the LLM, no GPU."""

    def transcribe(self, audio_ref, language=None):
        return SPOKEN

    def diarize(self, audio_ref):
        return list(TURNS)


class SessionCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "store").mkdir()
        create_key_file(root / "key" / "norev.key")
        self.audio = root / "meeting.wav"
        self.audio.write_bytes(b"not really audio")
        self.env = {"NOREV_STORE_PATH": str(root / "store" / "norev.db"),
                    "NOREV_STORE_KEY_FILE": str(root / "key" / "norev.key")}
        self.processor = FakeProcessor([])

    def tearDown(self):
        self.tmp.cleanup()

    def _answers(self, session_id, *translations):
        """Recorded translation answers, then the Domain stage answer for this Session's ids."""
        domain = json.dumps(ANSWERS["corporate"]).replace('"s1-1"', f'"{session_id}-1"')
        self.processor = FakeProcessor([json.dumps(t) for t in translations] + [domain])

    def _main(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env if env is not None else self.env, clear=True), \
                patch.object(cli, "_processor", lambda store, **models: self.processor), \
                patch.object(cli, "LegacyActionExtractor", lambda *a, **k: FakeActionExtractor(GENERIC)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_transcribe_analyse_show(self):
        code, out, _ = self._main("transcribe", str(self.audio), "--date", "2026-11-06")
        self.assertEqual(code, 0)
        session_id = out.strip()
        self._answers(session_id, TRANSLATION)
        self.assertEqual(self._main("analyse", session_id)[0], 0)
        code, out, _ = self._main("show", session_id)
        self.assertEqual(code, 0)
        self.assertIn("Domain: corporate | 2026-11-06", out)
        self.assertIn("Speakers: speaker_0, speaker_1", out)
        self.assertIn("[proposed] work_item: Send the report", out)
        self.assertNotIn("PARTIAL", out)

        with patch.dict(os.environ, self.env, clear=True):
            store = open_store(StoreSettings.from_environment())
        try:
            self.assertEqual(store.get_session(session_id).domain, "corporate")
        finally:
            store.close()
        self.assertEqual(os.listdir(Path(self.env["NOREV_STORE_PATH"]).parent), ["norev.db"])

    def test_untranslated_turns_are_shown_as_partial(self):
        session_id = self._main("transcribe", str(self.audio))[1].strip()
        self._answers(session_id, {"turns": [TRANSLATION["turns"][0]]}, {"turns": []})
        code, _, err = self._main("analyse", session_id)
        self.assertEqual(code, 0)
        self.assertIn("1 turn(s) not translated; the result is partial", err)
        out = self._main("show", session_id)[1]
        self.assertIn("PARTIAL: 1 turn(s) could not be translated", out)
        self.assertIn("speaker_1] Bine, mulțumesc.", out)

    def test_unknown_domain_and_others_are_refused_before_anything_runs(self):
        for domain, message in (("finance", "unknown Domain"), ("others", "#21")):
            with self.subTest(domain=domain):
                code, out, err = self._main("transcribe", str(self.audio), "--domain", domain)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(message, err)
        self.assertFalse(Path(self.env["NOREV_STORE_PATH"]).exists())

    def test_without_a_key_nothing_is_written(self):
        code, _, err = self._main("transcribe", str(self.audio), env={"NOREV_STORE_PATH": self.env["NOREV_STORE_PATH"]})
        self.assertEqual(code, 2)
        self.assertIn("NOREV_STORE_KEY_FILE", err)
        self.assertFalse(Path(self.env["NOREV_STORE_PATH"]).exists())

    def test_missing_audio_file_creates_no_session(self):
        code, out, err = self._main("transcribe", str(self.audio) + ".missing")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("FileNotFoundError: no file", err)

    def test_analyse_before_transcribe_says_so(self):
        with patch.dict(os.environ, self.env, clear=True):
            store = open_store(StoreSettings.from_environment())
        try:
            store.save_session(Session("s-new", datetime(2026, 11, 6, tzinfo=timezone.utc), "corporate", audio_ref="a"))
        finally:
            store.close()
        code, _, err = self._main("analyse", "s-new")
        self.assertEqual(code, 1)
        self.assertIn("not transcribed yet; run `transcribe` first", err)

    def test_failure_is_one_line_without_model_details(self):
        session_id = self._main("transcribe", str(self.audio))[1].strip()  # no answers: the translation call fails
        code, _, err = self._main("analyse", session_id)
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "norev.session analyse failed: AssertionError: details withheld")


class LegacyPipelineTest(unittest.TestCase):
    def test_translate_and_extract_warn_they_are_deprecated(self):
        for stage in ("translate", "extract"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                pipeline.main([stage, "missing.json"])
            self.assertIn("[deprecated]", err.getvalue())


if __name__ == "__main__":
    unittest.main()
