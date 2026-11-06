"""LLM stages of the norev pipeline, run one at a time against the llama-server that norev.sh started.

  python -m LLM.pipeline translate <transcript.json>   -> Outputs/Audio/<name>.en.json (prints path)
  python -m LLM.pipeline extract <transcript.json>     -> Outputs/Json/<name>.pm_tasks.json (prints path)
  python -m LLM.pipeline show <transcript.json> <pm_tasks.json>
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from LLM.AI_Model import deepseek
from LLM.AI_Model.translate import TranscriptTranslator, split_sentences
from LLM.Whisper.settings import WhisperSettings


# Concurrent requests per stage; must match llama-server --parallel (norev.sh sets both)
WORKERS = max(1, int(os.getenv("LLM_PARALLEL", "1")))


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def translate(transcript_path: str) -> int:
    transcript = _load(transcript_path)
    source = transcript.get("detected_language") or ""
    translator = TranscriptTranslator()
    started = time.perf_counter()
    text, chunks = translator.translate(transcript.get("text", ""), source=source, workers=WORKERS)

    translated = {
        **transcript,
        "text": text,
        "segments": [],  # timestamps no longer line up with the translated text
        "detected_language": "en",
        "source_language": source,
        "source_text": transcript.get("text", ""),
        "translation_model": translator.llm_model,
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
    extractor = deepseek.RealtimeTaskExtractor(output_path=output_path)
    segments = [s.get("text", "") for s in transcript.get("segments") or []]
    extractor.extract_batch(segments or split_sentences(transcript.get("text", "")), workers=WORKERS)

    transcript["pm_tasks_output_file"] = str(output_path)
    transcript["pm_tasks_llm_model"] = extractor.llm_model
    transcript["pm_tasks_elapsed_seconds"] = round(time.perf_counter() - started, 3)
    Path(transcript_path).write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_path)
    return 0


def show(transcript_path: str, pm_tasks_path: str) -> int:
    data = _load(transcript_path)
    pm = _load(pm_tasks_path)
    source = data.get("source_language")
    language = f"{source} -> en" if source else data.get("detected_language")
    print()
    print(
        f"Language: {language} | device: {data.get('runtime_device')}"
        f" | transcription: {data.get('transcription_elapsed_seconds')}s"
        + (f" | translation: {data['translation_elapsed_seconds']}s" if source else "")
        + f" | tasks: {data.get('pm_tasks_elapsed_seconds')}s"
    )
    print("Tasks file:", pm_tasks_path)
    if pm.get("meeting_summary"):
        print("\nSummary:\n ", pm["meeting_summary"])
    tasks = pm.get("tasks") or []
    print(f"\nTasks ({len(tasks)}):")
    for task in tasks:
        who = task.get("assignee") or task.get("owner") or "-"
        due = task.get("due_date") or task.get("deadline") or "-"
        review = "  [verifică]" if task.get("needs_review") else ""
        print(f"  - [{task.get('priority', '-')}] {task.get('title')}  (who: {who}, due: {due}){review}")
    for key in ("decisions", "risks"):
        items = pm.get(key) or []
        if items:
            print(f"\n{key.capitalize()}:")
            for item in items:
                print("  -", item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="norev LLM stages")
    sub = parser.add_subparsers(dest="stage", required=True)
    sub.add_parser("translate").add_argument("transcript_json")
    sub.add_parser("extract").add_argument("transcript_json")
    show_parser = sub.add_parser("show")
    show_parser.add_argument("transcript_json")
    show_parser.add_argument("pm_tasks_json")
    args = parser.parse_args(argv)

    try:
        if args.stage == "translate":
            return translate(args.transcript_json)
        if args.stage == "extract":
            return extract(args.transcript_json)
        return show(args.transcript_json, args.pm_tasks_json)
    except Exception as exc:
        print(json.dumps({"error": f"{args.stage} failed: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
