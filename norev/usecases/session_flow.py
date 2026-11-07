"""Session flow (#19): finished audio -> Transcript with Speakers (usecases.transcribe, #24) ->
English per Speaker turn -> generic extraction -> Speaker Roles (#60) -> Domain stage -> Proposed
Actions waiting for review.

Extraction runs only here, after the final Transcript (no Live Transcript extraction). Every model
call goes through the `translator`, `extractor` and `llm` the composition root built on the
Central Processor; this module only orders the stages and saves through the SessionStore.
"""
import dataclasses
from dataclasses import dataclass

from norev.core.models import ActionStatus, Domain, OthersPrompt, ProposedAction, Segment, Session, Suggestion
from norev.core.ports import ActionExtractor, LlmClient, SessionStore, Translator
from norev.usecases.domain_stage import ask_domain
from norev.usecases.extract import to_proposed_actions
from norev.usecases.others_completion import FAILED, check_template, complete
from norev.usecases.others_prompt import others_domain
from norev.usecases.speaker_roles import propose_speaker_roles

DEFAULT_DOMAIN = "corporate"
DOMAINS = ("corporate", "medical", "others")


def select_domain(name: str | None = None) -> str:
    """Corporate unless another Domain is chosen explicitly; an unknown name is an error, never a fallback."""
    if name is None:
        return DEFAULT_DOMAIN
    if name not in DOMAINS:
        raise ValueError(f"unknown Domain {name!r}; choose one of {', '.join(DOMAINS)}")
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
    regenerate: bool = False,
    others_prompt: OthersPrompt | None = None,
    completion: tuple[str, dict] | None = None,
) -> SessionResult:
    """Analyse a Session transcribed by usecases.transcribe.transcribe_session and save the result for review.

    The first run translates the spoken Transcript per Speaker turn (unless it is English or
    `translate` is False), keeps the spoken one as `source_transcript_ref` and points
    `transcript_ref` at the one extraction runs on. Later runs reuse it and regenerate only the
    Proposed Actions; that is refused once the User decided any of them.
    `regenerate` is the User's explicit request to analyse again, possibly with another Domain:
    the Proposed actions are replaced, every decided one is kept unchanged. Changing the Domain is
    refused while an action waits for execution (approved or failed): its plan needs its own Domain.
    Without `regenerate`, another Domain or a decided action raises ValueError and changes nothing.
    Domain "others" runs with the Session's confirmed prompt (#21), never one made up here; a newly
    confirmed `others_prompt` replaces it only with `regenerate`, in the same write as the actions.
    Speaker Roles are proposed before the Domain stage (a Role the User confirmed is kept); an invalid
    answer leaves them unknown and the analysis goes on.
    `completion` (#83) is the Others completion template and schema: with a requested result, one more
    model call after the Domain stage; an invalid answer leaves the result empty with a Suggestion.
    The result is saved with every analysis ("" without a request), so an old one never stays.
    A failing stage raises and leaves what was saved before it; nothing is ever executed here.
    """
    session = _session(session_id, store)
    if session.domain != domain.name and not regenerate:
        raise ValueError(f"Session {session_id!r} is in Domain {session.domain!r}, not {domain.name!r}; "
                         "changing it needs an explicit regeneration")
    if not session.transcript_ref:
        raise LookupError(f"Session {session_id!r} is not transcribed yet")
    if others_prompt is not None and others_prompt != session.others_prompt and not regenerate:
        raise ValueError(f"Session {session_id!r}: a new Others prompt needs an explicit regeneration")
    if domain.name == "others":
        prompt = others_prompt or session.others_prompt
        if prompt is None:
            raise ValueError(f"Session {session_id!r}: Domain 'others' needs a prompt confirmed by the User")
        domain = others_domain(domain, prompt)
        if prompt.completion:  # before any model call, as the Domain prompt
            if completion is None:
                raise ValueError(f"Session {session_id!r}: the Others result needs the completion template")
            check_template(completion[0], prompt)

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
    decided = [a for a in snapshot.actions if a.status is not ActionStatus.PROPOSED]
    if decided and not regenerate:
        raise ValueError(f"Session {session_id!r} already has decided Proposed Actions; regenerate explicitly")
    pending = [a.id for a in decided if a.status in (ActionStatus.APPROVED, ActionStatus.FAILED)]
    if session.domain != domain.name and pending:
        raise ValueError(f"Session {session_id!r}: execute or resolve {', '.join(pending)} before changing the Domain")
    started = session.started_at
    session_date = (started.astimezone() if started.tzinfo else started).date()  # local day of the Session
    # A regeneration numbers its actions by revision, so new ids never collide with kept ones
    prefix = f"{session_id}-r{snapshot.revision}" if regenerate else session_id
    raw = extractor.extract(transcript, session_date)
    generic = to_proposed_actions(raw, id_prefix=prefix)
    # Domain's model: it is loaded next for the Domain stage anyway
    speakers = propose_speaker_roles(session_id, store, domain, llm, model=domain.model)
    actions, suggestions = ask_domain(domain, llm, transcript, generic, session_date, speakers, id_prefix=prefix)
    result = ""
    if domain.name == "others" and prompt.completion:
        result = complete(llm, *completion, prompt, transcript, session_date, model=domain.model)
        if result is None:
            result, suggestions = "", [*suggestions, FAILED]
    if regenerate:
        store.regenerate_actions(session_id, decided + actions, domain=domain.name,
                                 expected_revision=snapshot.revision, suggestions=suggestions,
                                 others_prompt=others_prompt)
    else:
        store.save_actions(session_id, actions, expected_revision=snapshot.revision, suggestions=suggestions)
    summary = raw.get("session_summary") if isinstance(raw, dict) else None
    store.save_summary(session_id, summary.strip() if isinstance(summary, str) else "")  # the last analysis's (#59)
    store.save_completion(session_id, result)
    return SessionResult(actions, suggestions, tuple(s for s in transcript.segments if s.untranslated))


def _session(session_id: str, store: SessionStore) -> Session:
    session = store.get_session(session_id)
    if session is None:
        raise LookupError(f"no Session {session_id!r}")
    return session
