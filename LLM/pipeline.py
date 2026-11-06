"""LLM stages of the norev pipeline, run one at a time against the llama-server that norev.sh started.

  python -m LLM.pipeline diarize <transcript.json>     -> adds speaker_id to the segments (needs NeMo)
  python -m LLM.pipeline translate <transcript.json>   -> Outputs/Audio/<name>.en.json (prints path)
  python -m LLM.pipeline extract <transcript.json>     -> Outputs/Json/<name>.proposed_actions.json (prints path)
  python -m LLM.pipeline owners <transcript.json> <proposed_actions.json>  -> owners from named Speakers
  python -m LLM.pipeline show <transcript.json> <proposed_actions.json>
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from LLM.AI_Model import deepseek
from LLM.AI_Model.client import build_llm_client
from LLM.AI_Model.settings import DeepSeekSettings
from LLM.AI_Model.translate import TranscriptTranslator, split_sentences
from LLM.Whisper.settings import WhisperSettings
from norev.adapters.llm.speaker_names import SpeakerNamer, Turn, speaker_owners
from norev.adapters.llm.translator import TurnTranslator, speaker_turns
from norev.core.models import Segment, Transcript


# Concurrent requests per stage; must match llama-server --parallel (norev.sh sets both)
WORKERS = max(1, int(os.getenv("LLM_PARALLEL", "1")))


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _has_speakers(transcript: dict) -> bool:
    """At least two Speakers: with one voice, who said what adds nothing and the old path is kept."""
    return len({s.get("speaker_id") for s in transcript.get("segments") or []} - {None}) >= 2


def _core_transcript(transcript: dict) -> Transcript:
    segments = tuple(
        Segment(s["start"], s["end"], s.get("text", ""), s.get("speaker_id")) for s in transcript.get("segments") or []
    )
    return Transcript(segments=segments, language=transcript.get("detected_language"))


def diarize(transcript_path: str) -> int:
    """Who spoke when (NVIDIA Sortformer), then the Speaker of every Whisper word; the segments are
    regrouped so that each one holds a single Speaker, named S1, S2... in order of first appearance."""
    from norev.adapters.diarization.overlap import assign_speakers, split_by_speaker
    from norev.adapters.diarization.sortformer import SortformerDiarizer

    transcript = _load(transcript_path)
    started = time.perf_counter()
    turns = SortformerDiarizer().diarize(transcript["input_file"])
    segments = transcript.get("segments") or []
    words = [(w["start"], w["end"], w["word"]) for s in segments for w in s.get("words") or []]
    if words:
        spans = split_by_speaker(words, turns)
    else:  # Whisper ran without word timestamps: one Speaker per segment
        speakers = assign_speakers([(s["start"], s["end"]) for s in segments], turns)
        spans = [(s["start"], s["end"], spk, s["text"]) for s, spk in zip(segments, speakers)]
    names: dict[str, str] = {}
    for _, _, speaker, _ in spans:
        if speaker is not None and speaker not in names:
            names[speaker] = f"S{len(names) + 1}"
    transcript["segments"] = [
        {"start": round(a, 3), "end": round(b, 3), "speaker_id": names.get(spk), "text": text}
        for a, b, spk, text in spans if text
    ] or segments
    transcript["speakers"] = list(names.values())
    transcript["diarization_elapsed_seconds"] = round(time.perf_counter() - started, 3)
    Path(transcript_path).write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    print(len(names))
    return 0


def translate(transcript_path: str) -> int:
    transcript = _load(transcript_path)
    source = transcript.get("detected_language") or ""
    started = time.perf_counter()
    if _has_speakers(transcript):
        # Per Speaker turn, so who said what survives the translation (decision #13)
        settings = DeepSeekSettings.from_environment()
        english = TurnTranslator(build_llm_client(settings), model=settings.model, workers=WORKERS).translate(
            _core_transcript(transcript), source
        )
        segments = [
            {"start": s.start, "end": s.end, "speaker_id": s.speaker_id, "text": s.text, "untranslated": s.untranslated}
            for s in english.segments
        ]
        text, chunks, model = english.text, len(segments), settings.model
    else:
        translator = TranscriptTranslator()
        text, chunks = translator.translate(transcript.get("text", ""), source=source, workers=WORKERS)
        segments, model = [], translator.llm_model  # timestamps no longer line up with the translated text

    translated = {
        **transcript,
        "text": text,
        "segments": segments,
        "detected_language": "en",
        "source_language": source,
        "source_text": transcript.get("text", ""),
        "translation_model": model,
        "translation_chunks": chunks,
        "translation_elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    source_path = Path(transcript_path)
    output_path = source_path.with_name(f"{source_path.stem}.en.json")
    output_path.write_text(json.dumps(translated, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_path)
    return 0


def extract(transcript_path: str) -> int:
    transcript = _load(transcript_path)
    output_path = deepseek.output_path_for_input(
        transcript.get("input_file") or transcript_path, WhisperSettings.DEFAULT_OUTPUT_DIR
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    extractor = deepseek.RealtimeActionExtractor(output_path=output_path)
    # Sentences, not segments: a translated Speaker turn can be very long (one Speaker, or no diarization)
    sentences = [x for s in transcript.get("segments") or [] for x in split_sentences(s.get("text", ""))]
    extractor.extract_batch(sentences or split_sentences(transcript.get("text", "")), workers=WORKERS)

    transcript["proposed_actions_output_file"] = str(output_path)
    transcript["proposed_actions_llm_model"] = extractor.llm_model
    transcript["proposed_actions_elapsed_seconds"] = round(time.perf_counter() - started, 3)
    Path(transcript_path).write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_path)
    return 0


def owners(transcript_path: str, proposed_actions_path: str) -> int:
    """Name the Speakers from the Transcript, then give "I'll do it" Proposed Actions to the Speaker
    who said it, only when that Speaker spoke before and their name was already known. The owner the
    extractor guessed for such an action is kept as extracted_owner."""
    transcript = _load(transcript_path)
    result = _load(proposed_actions_path)
    turns = [Turn(t.speaker_id, t.text) for t in speaker_turns(_core_transcript(transcript)) if t.speaker_id]
    settings = DeepSeekSettings.from_environment()
    names = SpeakerNamer(build_llm_client(settings), model=settings.model, workers=WORKERS).name(turns)
    assigned = speaker_owners(turns, result.get("proposed_actions") or [], names)
    for action in result.get("proposed_actions") or []:
        if action.get("id") in assigned:
            name, speaker_id = assigned[action["id"]]
            if action.get("owner") and action["owner"] != name:
                action["extracted_owner"] = action["owner"]
            action["owner"], action["owner_speaker_id"] = name, speaker_id
    result["speakers"] = {
        s: {"name": n.name, "evidence": turns[n.evidence_turn].text} for s, n in sorted(names.items())
    }
    Path(proposed_actions_path).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(len(assigned))
    return 0


def show(transcript_path: str, proposed_actions_path: str) -> int:
    data = _load(transcript_path)
    result = _load(proposed_actions_path)
    source = data.get("source_language")
    language = f"{source} -> en" if source else data.get("detected_language")
    print()
    print(
        f"Language: {language} | device: {data.get('runtime_device')}"
        f" | transcription: {data.get('transcription_elapsed_seconds')}s"
        + (f" | translation: {data['translation_elapsed_seconds']}s" if source else "")
        + f" | Proposed Actions: {data.get('proposed_actions_elapsed_seconds')}s"
    )
    print("Proposed Actions file:", proposed_actions_path)
    speakers = data.get("speakers") or []
    if speakers:
        named = result.get("speakers") or {}
        print("Speakers:", ", ".join(f"{s} = {named[s]['name']}" if s in named else f"{s} = ?" for s in speakers))
    if result.get("session_summary"):
        print("\nSession summary:\n ", result["session_summary"])
    actions = result.get("proposed_actions") or []
    print(f"\nProposed Actions ({len(actions)}):")
    for action in actions:
        who = action.get("assignee") or action.get("owner") or "-"
        due = action.get("due_date") or action.get("deadline") or "-"
        review = "  [verifică]" if action.get("needs_review") else ""
        said = f" [{action['owner_speaker_id']}, said it]" if action.get("owner_speaker_id") else ""
        if action.get("extracted_owner"):
            said += f" [the extractor had guessed {action['extracted_owner']}]"
        print(f"  - [{action.get('priority', '-')}] {action.get('title')}  (who: {who}{said}, due: {due}){review}")
    for key in ("decisions", "risks"):
        items = result.get(key) or []
        if items:
            print(f"\n{key.capitalize()}:")
            for item in items:
                print("  -", item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="norev LLM stages")
    sub = parser.add_subparsers(dest="stage", required=True)
    for stage in ("diarize", "translate", "extract"):
        sub.add_parser(stage).add_argument("transcript_json")
    for stage in ("owners", "show"):
        stage_parser = sub.add_parser(stage)
        stage_parser.add_argument("transcript_json")
        stage_parser.add_argument("proposed_actions_json")
    args = parser.parse_args(argv)

    try:
        if args.stage == "diarize":
            return diarize(args.transcript_json)
        if args.stage == "translate":
            return translate(args.transcript_json)
        if args.stage == "extract":
            return extract(args.transcript_json)
        if args.stage == "owners":
            return owners(args.transcript_json, args.proposed_actions_json)
        return show(args.transcript_json, args.proposed_actions_json)
    except Exception as exc:
        print(json.dumps({"error": f"{args.stage} failed: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
