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
from norev.launcher.llama import ModelMissing
from norev.testing.fakes import FakeActionExtractor, FakeLlmClient
from test_usecases_session_flow import GENERIC, ROLES, SPOKEN, TURNS


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

    def _answers(self, *minutes):
        """Recorded answers for Local council: Speaker Roles, the agenda-and-votes pass, then per fragment the
        follow-ups and the minutes. No translation: the minutes are written from the spoken Transcript."""
        empty = json.dumps({"proposed_actions": [], "added_actions": [], "suggestions": []})
        fragments = [answer for text in minutes for answer in (empty, json.dumps({"output": text}))]
        agenda = {"meeting": {"locality": "", "type": "", "attendance": "", "chair": ""}, "items": []}
        self.processor = FakeProcessor([json.dumps(ROLES), json.dumps(agenda), *fragments])

    def _fake_processor(self, store, **models):
        self.models = models  # what the CLI would give the real Central Processor
        return self.processor

    def _main(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env if env is not None else self.env, clear=True), \
                patch.object(cli, "_processor", self._fake_processor), \
                patch.object(cli, "LegacyActionExtractor", lambda *a, **k: FakeActionExtractor(GENERIC)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_transcribe_analyse_show(self):
        code, out, _ = self._main("transcribe", str(self.audio), "--date", "2026-11-06")
        self.assertEqual(code, 0)
        session_id = out.strip()
        self._answers("Deschiderea ședinței și prezența.")
        self.assertEqual(self._main("analyse", session_id)[0], 0)
        code, out, _ = self._main("show", session_id)
        self.assertEqual(code, 0)
        self.assertIn("Domain: local_council | 2026-11-06", out)
        self.assertIn("Speakers: S1, S2", out)
        self.assertIn("Proces-verbal (draft, de verificat):", out)
        self.assertIn("Deschiderea ședinței și prezența.", out)
        self.assertIn("## Hotărâri și voturi", out)  # no vote found: flagged, never invented
        self.assertNotIn("PARTIAL", out)

        with patch.dict(os.environ, self.env, clear=True):
            store = open_store(StoreSettings.from_environment())
        try:
            self.assertEqual(store.get_session(session_id).domain, "local_council")
        finally:
            store.close()
        self.assertEqual(os.listdir(Path(self.env["NOREV_STORE_PATH"]).parent), ["norev.db"])
        self.assertNotIn(b"not really audio", Path(self.env["NOREV_STORE_PATH"]).read_bytes())  # audio sealed

    def test_the_minutes_are_written_from_the_spoken_romanian_without_translation(self):
        session_id = self._main("transcribe", str(self.audio))[1].strip()
        self._answers("Proces-verbal.")
        self.assertEqual(self._main("analyse", session_id)[0], 0)
        prompts = " ".join(call[0] for call in self.processor.calls)
        for segment in SPOKEN.segments:
            self.assertIn(segment.text, prompts)
        self.assertNotIn("PARTIAL", self._main("show", session_id)[1])

    def test_only_local_council_and_romanian_are_accepted_before_anything_runs(self):
        # this version: argparse refuses any other Domain or language
        for args in (("--domain", "corporate"), ("--domain", "medical"), ("--domain", "others"), ("--language", "en")):
            with self.subTest(args=args), self.assertRaises(SystemExit) as raised:
                self._main("transcribe", str(self.audio), *args)
            self.assertEqual(raised.exception.code, 2)
        self.assertFalse(Path(self.env["NOREV_STORE_PATH"]).exists())

    def test_without_a_key_nothing_is_written(self):
        code, _, err = self._main("transcribe", str(self.audio), env={"NOREV_STORE_PATH": self.env["NOREV_STORE_PATH"]})
        self.assertEqual(code, 2)
        self.assertIn("NOREV_STORE_KEY_FILE", err)
        self.assertFalse(Path(self.env["NOREV_STORE_PATH"]).exists())

    def test_no_speakers_skips_diarization(self):
        self.assertEqual(self._main("transcribe", str(self.audio), "--no-speakers")[0], 0)
        self.assertEqual(self.models["diarizer"].diarize("any"), [])

    def test_missing_audio_file_creates_no_session(self):
        code, out, err = self._main("transcribe", str(self.audio) + ".missing")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("FileNotFoundError: no file", err)

    def test_analyse_before_transcribe_says_so(self):
        with patch.dict(os.environ, self.env, clear=True):
            store = open_store(StoreSettings.from_environment())
        try:
            store.save_session(Session("s-new", datetime(2026, 11, 6, tzinfo=timezone.utc), "local_council", audio_ref="a"))
        finally:
            store.close()
        code, _, err = self._main("analyse", "s-new")
        self.assertEqual(code, 1)
        self.assertIn("not transcribed yet; run `transcribe` first", err)

    def test_with_llama_server_analyse_starts_the_models_and_always_stops_them(self):
        """#62: a missing stage model is named in the one-line error; llama-server is stopped anyway."""
        session_id = self._main("transcribe", str(self.audio))[1].strip()

        class Models:
            stopped = 0

            def stop(self):
                Models.stopped += 1

        class MissingModel(FakeProcessor):
            def generate_json(self, prompt, **kw):
                raise ModelMissing("modelul GGUF medgemma.gguf nu există")

        self.processor = MissingModel([])
        with patch("norev.launcher.llama.models_from_environment", lambda log_file: Models()):
            code, _, err = self._main("analyse", session_id, env={**self.env, "LLAMA_SERVER": "llama-server",
                                                                  "NOREV_HOME": self.tmp.name})
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "norev.session analyse failed: ModelMissing: modelul GGUF medgemma.gguf nu există")
        self.assertIsInstance(self.models["models"], Models)
        self.assertEqual(Models.stopped, 1)

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
