"""Record, or check, the LLM responses for the synthetic Sessions in this folder.

    python tests/fixtures/llm/record.py           # needs llama-server (GPU laptop); rewrites the fixtures
    python tests/fixtures/llm/record.py --check   # no model: replays the recorded responses

For each consult_*/transcript.json it runs the existing pipeline the way norev.sh does
(translate to English, then generic Proposed Action extraction) and writes:
  responses.json         every LLM call in order: stage, prompt, JSON schema, response text
  proposed_actions.json  what the extractor produced from those responses

--check replays responses.json through the current code and fails if a prompt or the result
changed, so a prompt change shows up as a fixture that needs re-recording.
Run from the repo root.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root, for the LLM package

# Same settings as norev.sh, so the recording matches a real run. One request at a time keeps
# the call order deterministic for replay.
os.environ.setdefault("LLM_PROVIDER", "llamacpp")
os.environ.setdefault("LLM_FORCE_JSON_FORMAT", "1")
os.environ.setdefault("LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT", "0")
os.environ.setdefault("LLM_TEMPERATURE", "0")
os.environ.setdefault("LLM_SECOND_PASS", "0")

from LLM.AI_Model import deepseek  # noqa: E402
from LLM.AI_Model.client import build_llm_client  # noqa: E402
from LLM.AI_Model.settings import DeepSeekSettings  # noqa: E402
from LLM.AI_Model.translate import TranscriptTranslator, split_sentences  # noqa: E402

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


def run(transcript: dict, client) -> dict:
    os.environ["LLM_SESSION_DATE"] = transcript["session_date"]
    settings = DeepSeekSettings.from_environment()
    text = " ".join(s["text"] for s in transcript["segments"])

    client.stage = "translate"
    english, _ = TranscriptTranslator(settings=settings, client=client).translate(
        text, source=transcript["language"], workers=1
    )

    client.stage = "extract"
    with tempfile.TemporaryDirectory() as tmp:
        extractor = deepseek.RealtimeActionExtractor(
            output_path=Path(tmp) / "out.json", settings=settings, client=client
        )
        result = extractor.extract_batch(split_sentences(english), workers=1)
    return json.loads(result)


def main(check: bool) -> int:
    failed = 0
    for folder in sorted(HERE.glob("consult_*")):
        transcript = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
        if check:
            recorded = json.loads((folder / "responses.json").read_text(encoding="utf-8"))
            client = ReplayClient(recorded["calls"])
            try:
                result = run(transcript, client)
                expected = json.loads((folder / "proposed_actions.json").read_text(encoding="utf-8"))
                assert not client._calls, "fewer LLM calls than recorded"
                assert result == expected, "result differs from proposed_actions.json"
                print(f"{folder.name}: OK")
            except AssertionError as exc:
                failed += 1
                print(f"{folder.name}: FAILED: {exc}")
            continue

        settings = DeepSeekSettings.from_environment()
        client = RecordingClient(build_llm_client(settings))
        result = run(transcript, client)
        recorded = {"model": settings.model, "session_date": transcript["session_date"], "calls": client.calls}
        (folder / "responses.json").write_text(json.dumps(recorded, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (folder / "proposed_actions.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{folder.name}: {len(client.calls)} calls, {len(result.get('proposed_actions', []))} Proposed Actions")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main("--check" in sys.argv[1:]))
