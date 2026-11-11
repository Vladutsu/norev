"""Record, or check, the LLM responses for the synthetic Sessions in this folder.

    python tests/fixtures/llm/record.py           # needs llama-server (GPU laptop); rewrites the fixtures
    python tests/fixtures/llm/record.py --check   # no model: replays the recorded responses
    python tests/fixtures/llm/record.py --refresh # no model: replays them and rewrites the results (code changed, prompts did not)

For each consult_*/transcript.json it runs the core pipeline: translation per Speaker turn
(norev.adapters.llm.translator, decision #13), then generic Proposed Action extraction
(norev.adapters.llm.legacy_extractor), and writes:
  responses.json         every LLM call in order: stage, prompt, JSON schema, response text
  transcript.en.json     the English Transcript, one Segment per Speaker turn, with speaker_id
  proposed_actions.json  what the extractor produced from those responses

--check replays responses.json through the current code and fails if a prompt or a result
changed, so a prompt change shows up as a fixture that needs re-recording.
Run from the repo root.
"""
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root, for the LLM and norev packages

# Same settings as norev.sh, so the recording matches a real run. One request at a time keeps
# the call order deterministic for replay.
os.environ.setdefault("LLM_PROVIDER", "llamacpp")
os.environ.setdefault("LLM_FORCE_JSON_FORMAT", "1")
os.environ.setdefault("LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT", "0")
os.environ.setdefault("LLM_TEMPERATURE", "0")
os.environ.setdefault("LLM_SECOND_PASS", "0")

from LLM.AI_Model.client import build_llm_client  # noqa: E402
from LLM.AI_Model.settings import DeepSeekSettings  # noqa: E402
from norev.adapters.llm.legacy_extractor import LegacyActionExtractor  # noqa: E402
from norev.adapters.llm.translator import TurnTranslator  # noqa: E402
from norev.core.models import Segment, Transcript  # noqa: E402

HERE = Path(__file__).parent


class RecordingClient:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.stage = ""
        self.calls: list[dict] = []

    def generate_json(self, prompt, *, model=None, json_schema=None):
        response = self._inner.generate_json(prompt, model=model, json_schema=json_schema)
        self.calls.append({"stage": self.stage, "prompt": prompt, "json_schema": json_schema, "response": response})
        return response


class ReplayClient:
    """Like norev.testing.fakes.FakeLlmClient, but also checks each prompt against the recording."""

    def __init__(self, calls: list[dict]) -> None:
        self._calls = list(calls)
        self.stage = ""

    def generate_json(self, prompt, *, model=None, json_schema=None):
        if not self._calls:
            raise AssertionError("more LLM calls than recorded")
        call = self._calls.pop(0)
        if (call["prompt"], call["json_schema"]) != (prompt, json_schema):
            raise AssertionError(f"{call['stage']} prompt or schema differs from the recording")
        return call["response"]


def load_transcript(data: dict) -> Transcript:
    """transcript.json or transcript.en.json -> core Transcript."""
    segments = tuple(
        Segment(s["start"], s["end"], s["text"], s["speaker_id"], untranslated=s.get("untranslated", False))
        for s in data["segments"]
    )
    return Transcript(segments=segments, language=data["language"])


def english_json(source: dict, english: Transcript) -> dict:
    return {
        "synthetic": source["synthetic"],
        "session_date": source["session_date"],
        "language": english.language,
        "source_language": source["language"],
        "speakers": source["speakers"],
        "segments": [
            {"start": s.start, "end": s.end, "speaker_id": s.speaker_id, "text": s.text, "untranslated": s.untranslated}
            for s in english.segments
        ],
    }


def run(source: dict, client, settings: DeepSeekSettings) -> tuple[dict, dict]:
    client.stage = "translate"
    english = TurnTranslator(client, model=settings.model).translate(load_transcript(source), source["language"])

    client.stage = "extract"
    actions = LegacyActionExtractor(client=client, settings=settings).extract(
        english, date.fromisoformat(source["session_date"])
    )
    return english_json(source, english), actions


def main(check: bool, refresh: bool = False) -> int:
    failed = 0
    settings = DeepSeekSettings.from_environment()
    for folder in sorted(HERE.glob("consult_*")):
        source = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
        if check:
            recorded = json.loads((folder / "responses.json").read_text(encoding="utf-8"))
            client = ReplayClient(recorded["calls"])
            try:
                english, actions = run(source, client, settings.with_overrides(model=recorded["model"]))
                assert not client._calls, "fewer LLM calls than recorded"
                if refresh:
                    for name, data in (("transcript.en.json", english), ("proposed_actions.json", actions)):
                        (folder / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                assert english == json.loads((folder / "transcript.en.json").read_text(encoding="utf-8")), \
                    "result differs from transcript.en.json"
                assert actions == json.loads((folder / "proposed_actions.json").read_text(encoding="utf-8")), \
                    "result differs from proposed_actions.json"
                print(f"{folder.name}: OK")
            except AssertionError as exc:
                failed += 1
                print(f"{folder.name}: FAILED: {exc}")
            continue

        client = RecordingClient(build_llm_client(settings))
        english, actions = run(source, client, settings)
        recorded = {"model": settings.model, "session_date": source["session_date"], "calls": client.calls}
        for name, data in (("responses.json", recorded), ("transcript.en.json", english), ("proposed_actions.json", actions)):
            (folder / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        stages = [c["stage"] for c in client.calls]
        untranslated = sum(s["untranslated"] for s in english["segments"])
        print(
            f"{folder.name}: {stages.count('translate')} translate + {stages.count('extract')} extract calls, "
            f"{len(english['segments'])} turns ({untranslated} untranslated), "
            f"{len(actions.get('proposed_actions', []))} Proposed Actions"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    refresh = "--refresh" in sys.argv[1:]
    raise SystemExit(main("--check" in sys.argv[1:] or refresh, refresh))
