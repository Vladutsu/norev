"""Session flow (#19): finished audio -> Transcript with Speakers (usecases.transcribe, #24) ->
English per Speaker turn -> generic extraction -> Domain stage -> Proposed Actions waiting for review.

Extraction runs only here, after the final Transcript (no Live Transcript extraction). Every model
call goes through the `translator`, `extractor` and `llm` the composition root built on the
Central Processor; this module only orders the stages and saves through the SessionStore.
"""
import dataclasses
from dataclasses import dataclass

from norev.core.models import ActionStatus, Domain, ProposedAction, Segment, Session, Suggestion
from norev.core.ports import ActionExtractor, LlmClient, SessionStore, Translator
from norev.usecases.domain_stage import ask_domain
from norev.usecases.extract import to_proposed_actions

DEFAULT_DOMAIN = "corporate"
DOMAINS = ("corporate", "medical", "others")


def select_domain(name: str | None = None) -> str:
    """Corporate unless another Domain is chosen explicitly; an unknown name is an error, never a fallback."""
    if name is None:
        return DEFAULT_DOMAIN
    if name not in DOMAINS:
        raise ValueError(f"unknown Domain {name!r}; choose one of {', '.join(DOMAINS)}")
    if name == "others":
        # shortcut: Others needs the User's confirmed prompt; remove this check when #21 lands
        raise ValueError("Domain 'others' needs a confirmed prompt from the User (#21), not available yet")
    return name


@dataclass(frozen=True)
class SessionResult:
    actions: list[ProposedAction]
    suggestions: list[Suggestion]
    # Turns the Translator could not translate: shown in the spoken language, so the result is partial
    untranslated: tuple[Segment, ...] = ()

    @property
    def partial(self) -> bool:
        return bool(self.untranslated)

    def __repr__(self) -> str:
        return f"SessionResult(actions={len(self.actions)}, suggestions={len(self.suggestions)}, untranslated={len(self.untranslated)})"


def run_session(
    session_id: str,
    *,
    store: SessionStore,
    translator: Translator,
    extractor: ActionExtractor,
    domain: Domain,
    llm: LlmClient,
    translate: bool = True,
) -> SessionResult:
    """Analyse a Session transcribed by usecases.transcribe.transcribe_session and save the result for review.

    The first run translates the spoken Transcript per Speaker turn (unless it is English or
    `translate` is False), keeps the spoken one as `source_transcript_ref` and points
    `transcript_ref` at the one extraction runs on. Later runs reuse it and regenerate only the
    Proposed Actions; that is refused once the User decided any of them.
    The Domain is fixed by the Session: running with another one raises ValueError and changes nothing.
    A failing stage raises and leaves what was saved before it; nothing is ever executed here.
    """
    session = _session(session_id, store)
    if session.domain != domain.name:
        raise ValueError(f"Session {session_id!r} is in Domain {session.domain!r}, not {domain.name!r}")
    if not session.transcript_ref:
        raise LookupError(f"Session {session_id!r} is not transcribed yet")

    transcript = store.load_transcript(session.transcript_ref)
    if not session.source_transcript_ref:  # first analysis: transcript_ref is still the spoken Transcript
        if translate and transcript.language != "en":
            transcript = translator.translate(transcript, transcript.language or "")
            ref = store.save_transcript(session_id, transcript)
        else:
            ref = session.transcript_ref
        session = dataclasses.replace(session, source_transcript_ref=session.transcript_ref, transcript_ref=ref)
        store.save_session(session)

    # Both stages run before one compare-and-set write: if either fails, the previous Proposed
    # Actions and Suggestions stay as they were (no generic-only list in between).
    snapshot = store.load_actions_snapshot(session_id)
    if any(a.status is not ActionStatus.PROPOSED for a in snapshot.actions):
        raise ValueError(f"Session {session_id!r} already has decided Proposed Actions")
    started = session.started_at
    session_date = (started.astimezone() if started.tzinfo else started).date()  # local day of the Session
    generic = to_proposed_actions(extractor.extract(transcript, session_date), id_prefix=session_id)
    actions, suggestions = ask_domain(domain, llm, transcript, generic, session_date)
    store.save_actions(session_id, actions, expected_revision=snapshot.revision, suggestions=suggestions)
    return SessionResult(actions, suggestions, tuple(s for s in transcript.segments if s.untranslated))


def _session(session_id: str, store: SessionStore) -> Session:
    session = store.get_session(session_id)
    if session is None:
        raise LookupError(f"no Session {session_id!r}")
    return session
