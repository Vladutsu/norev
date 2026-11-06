"""Run the Session flow on a recorded file, all locally, into the encrypted store (#19).

  python -m norev.session transcribe <audio_or_video> [--domain corporate|medical] [--language ro]
                                     [--date YYYY-MM-DD] [--no-speakers]     -> prints the Session id
  python -m norev.session analyse <session_id> [--no-translate]             (needs llama-server)
  python -m norev.session analyse <session_id> --regenerate [--domain medical]  analyse again, keep decisions
  python -m norev.session show <session_id>

transcribe runs Whisper and diarization and needs no LLM; analyse translates per Speaker turn,
extracts and runs the Domain stage. They are separate so Whisper and the LLM never hold the GPU at
the same time (./norev.sh starts llama-server between them). The Domain is Corporate unless chosen;
Others needs a prompt the User confirmed (#21), which the CLI cannot show, so it refuses Others. Needs NOREV_STORE_KEY_FILE: nothing is written in plaintext.
"""
import argparse
import os
import sys
import uuid
from datetime import date, datetime, time

from norev.adapters.domains.loader import DOMAINS, load_domain
from norev.adapters.llm.legacy_extractor import LegacyActionExtractor
from norev.adapters.llm.translator import TurnTranslator
from norev.adapters.store.encrypted import StoreKeyError, StoreSettings, open_store
from norev.core.models import Session
from norev.processor.compose import build_central_processor
from norev.usecases.session_flow import run_session, select_domain
from norev.usecases.transcribe import transcribe_session

# Concurrent LLM requests; must match llama-server --parallel (norev.sh sets both)
WORKERS = max(1, int(os.getenv("LLM_PARALLEL", "1")))


def _processor(store, **models):
    # The store is the audit log (with send results, #51); no Connector is configured,
    # so nothing can be sent from the CLI (execution is #23).
    return build_central_processor(store, **models)


class _NoSpeakers:
    """Diarizer for --no-speakers: no turns, so every Segment keeps no Speaker."""

    def diarize(self, audio_ref: str) -> list:
        return []


def transcribe(store, audio: str, domain: str, language: str | None, on: date | None, speakers: bool) -> str:
    if not os.path.isfile(audio):
        raise FileNotFoundError(f"no file {audio}")
    from norev.adapters.whisper.transcriber import WhisperTranscriber

    if speakers:
        from norev.adapters.diarization.sortformer import SortformerDiarizer

        diarizer = SortformerDiarizer(store)
    else:
        diarizer = _NoSpeakers()
    processor = _processor(store, transcriber=WhisperTranscriber(store), diarizer=diarizer)
    started = datetime.combine(on, time(12)).astimezone() if on else datetime.now().astimezone()
    session_id = f"s-{uuid.uuid4().hex[:10]}"
    with open(audio, "rb") as f:  # sealed in the store; Whisper and diarization decode it in memory
        audio_ref = store.save_audio(session_id, f.read())
    store.save_session(Session(session_id, started, domain, audio_ref=audio_ref))
    transcribe_session(session_id, store, processor, processor, language)
    return session_id


def analyse(store, session_id: str, translate: bool, regenerate: bool = False, domain: str | None = None):
    session = store.get_session(session_id)
    if session is None:
        raise LookupError(f"no Session {session_id!r}")
    if not session.transcript_ref:
        raise LookupError(f"Session {session_id!r} is not transcribed yet; run `transcribe` first")
    processor = _processor(store)
    model = os.getenv("LLM_MODEL") or None
    return run_session(
        session_id, store=store, domain=load_domain(DOMAINS / select_domain(domain or session.domain)),
        translator=TurnTranslator(processor, model=model, workers=WORKERS),
        extractor=LegacyActionExtractor(processor, workers=WORKERS), llm=processor, translate=translate,
        regenerate=regenerate,
    )


def show(store, session_id: str) -> None:
    session = store.get_session(session_id)
    if session is None:
        raise LookupError(f"no Session {session_id!r}")
    print(f"Session {session.id} | Domain: {session.domain} | {session.started_at:%Y-%m-%d}")
    if session.transcript_ref:
        transcript = store.load_transcript(session.transcript_ref)
        speakers = sorted({s.speaker_id for s in transcript.segments if s.speaker_id})
        print(f"Transcript: {transcript.language}, {len(transcript.segments)} turns, Speakers: {', '.join(speakers) or '-'}")
        untranslated = [s for s in transcript.segments if s.untranslated]
        if untranslated:
            print(f"PARTIAL: {len(untranslated)} turn(s) could not be translated and are kept in the spoken language:")
            for s in untranslated:
                print(f"  [{s.start:.1f}-{s.end:.1f} {s.speaker_id or '?'}] {s.text}")
        if not session.source_transcript_ref:
            print("Transcribed, not analysed yet: run `python -m norev.session analyse` with llama-server")
    actions = store.load_actions(session_id)
    print(f"\nProposed Actions ({len(actions)}), waiting for review:")
    for a in actions:
        fields = ", ".join(f"{k}: {v}" for k, v in a.fields.items() if v)
        print(f"  - [{a.status.value}] {a.action_type}: {a.title} (who: {a.owner or '-'}, due: {a.due_date or '-'})"
              + (f"\n      {fields}" if fields else ""))
    suggestions = store.load_suggestions(session_id)
    if suggestions:
        print("\nSuggestions:")
        for s in suggestions:
            print(f"  - {s.text}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m norev.session", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    t = sub.add_parser("transcribe")
    t.add_argument("audio")
    t.add_argument("--domain", default=None, help="corporate (default), medical or others")
    t.add_argument("--language", default=None, help="Whisper language code; default WHISPER_LANGUAGES or auto")
    t.add_argument("--date", type=date.fromisoformat, default=None, help="Session date for relative dates")
    t.add_argument("--no-speakers", action="store_true", help="skip diarization (no requirements-diarization.txt)")
    a = sub.add_parser("analyse")
    a.add_argument("session_id")
    a.add_argument("--no-translate", action="store_true", help="extract from the spoken language")
    a.add_argument("--regenerate", action="store_true",
                   help="analyse again: replace the Proposed actions, keep every decided one")
    a.add_argument("--domain", default=None, help="with --regenerate: analyse with another Domain")
    sub.add_parser("show").add_argument("session_id")
    args = parser.parse_args(argv)

    try:
        chosen = getattr(args, "domain", None)
        if args.command == "analyse" and chosen and not args.regenerate:
            raise ValueError("--domain on analyse needs --regenerate")
        domain = select_domain(chosen) if args.command == "transcribe" or chosen else None
        if domain == "others":  # the CLI has no way to show, edit and confirm the prompt (#21)
            raise ValueError("Domain 'others' needs a confirmed prompt from the User; not available from the CLI")
        store = open_store(StoreSettings.from_environment())
    except (ValueError, StoreKeyError) as e:
        print(f"norev.session: {e}", file=sys.stderr)
        return 2
    try:
        if args.command == "transcribe":
            print(transcribe(store, args.audio, domain, args.language, args.date, not args.no_speakers))
        elif args.command == "analyse":
            result = analyse(store, args.session_id, translate=not args.no_translate,
                             regenerate=args.regenerate, domain=domain)
            if result.partial:
                print(f"norev.session: {len(result.untranslated)} turn(s) not translated; the result is partial",
                      file=sys.stderr)
        else:
            show(store, args.session_id)
    except Exception as e:  # one line, no traceback: tracebacks and model errors can quote Session content
        detail = str(e) if isinstance(e, (LookupError, ValueError, OSError)) else "details withheld"
        print(f"norev.session {args.command} failed: {type(e).__name__}: {detail}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
