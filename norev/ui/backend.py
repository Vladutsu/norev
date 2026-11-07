"""What the desktop UI asks for, on the use cases of #18 (#27).

Until the full flow on the new core lands (#19), a Session is a demo: the synthetic meeting or
consultation in demo_session.json (one per Domain) is "recorded" and "analysed" here, then reviewed through the real approval use
cases (edit, preview, approve, reject) against the real store contract. Nothing is executed:
running a Workflow is #23's executor through the Central Processor, never the UI.
"""
import dataclasses
import hashlib
import json
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from norev.adapters.domains.loader import load_domain
from norev.core.models import (
    ActionStatus, Domain, ProposedAction, Segment, Session, Suggestion, Transcript, ValidationError,
)
from norev.core.ports import Clock, SessionStore
from norev.processor.payloads import email_preview
from norev.usecases.approve import approve_action, domain_digest, edit_action, preview_action, reject_action
from norev.usecases.transcript_correction import correct_transcript

ROOT = Path(__file__).resolve().parent.parent.parent
DEMO = Path(__file__).resolve().parent / "demo_session.json"
SECONDS_PER_SEGMENT = 1.6

MODES = (
    ("corporate", "Corporate", "Implicit"),
    ("medical", "Medical", "Exemplu"),
    ("others", "Others", "Prompt propriu"),
)
CONNECTOR_LABELS = {"google_calendar": "Google Calendar", "local_document": "Document local",
                    "email_draft": "Draft de email", "trello": "Trello", "jira": "Jira"}
LOCAL_CONNECTORS = ("local_document", "email_draft")  # need no account
GENERIC_FIELDS = ("title", "description", "owner", "due_date", "transcript_excerpt")


class ModeUnavailable(ValueError):
    """The mode's Domain is not in domains/ yet (Corporate: #20) or its analysis is not built (Others: #21)."""


class ExecutionPending(ValueError):
    """An approved Proposed Action still waits to execute: the Session is not deleted."""


class PromptNotConfirmed(ValueError):
    """Others needs the exact context and prompt the User confirmed."""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now().astimezone()


def prompt_digest(context: str, prompt: str) -> str:
    return hashlib.sha256(f"{context}\0{prompt}".encode()).hexdigest()


class SendsPayloadBuilder:
    """Plan of one payload per Connector step, holding exactly the step's `sends` fields.

    # shortcut: generic stand-in until #23's Connector-specific builders validate destinations.
    """

    def build(self, action: ProposedAction, domain: Domain, options: dict[str, Any]) -> dict[str, Any]:
        action_type = domain.action_type(action.action_type)
        steps = []
        for step in action_type.workflow.steps:
            if step.connector:
                steps.append({"step": step.name, "connector": step.connector,
                              "payload": {name: _value(action, name) for name in step.sends}})
        return {"version": 1, "domain": domain.name, "domain_digest": domain_digest(domain),
                "action_type": action.action_type, "steps": steps}


def _value(action: ProposedAction, name: str) -> Any:
    if name in GENERIC_FIELDS:
        value = getattr(action, name)
        return value.isoformat() if isinstance(value, date) else value
    return action.fields.get(name)


class DemoBackend:
    can_execute = False  # the demo reviews only; SessionBackend executes through the Central Processor (#43)
    google = None  # SessionBackend: the GoogleAccount from the composition root (#45); none in the demo
    trello = None  # SessionBackend: the TrelloLink from the composition root (#72); none in the demo
    jira = None  # SessionBackend: the JiraAccount from the composition root (#73); none in the demo

    def __init__(self, store: SessionStore, *, approved_by: str, clock: Clock | None = None,
                 domains_dir: Path = ROOT / "domains", monotonic=time.monotonic) -> None:
        self.store = store
        self.approved_by = approved_by
        self.clock = clock or SystemClock()
        self.domains_dir = domains_dir
        self.monotonic = monotonic
        self.demos = json.loads(DEMO.read_text(encoding="utf-8"))
        self.builder = SendsPayloadBuilder()
        # shortcut: kept in memory until the store has Speakers/Roles (#24), Others prompts (#21) and a Session list.
        self._sessions: dict[str, dict[str, Any]] = {}
        self._confirmed: set[str] = set()

    # --- modes and Others

    def modes(self) -> list[dict[str, Any]]:
        return [{"id": mode, "label": label, "hint": hint, "available": self._available(mode)}
                for mode, label, hint in MODES]

    def _available(self, mode: str) -> bool:
        return mode == "others" or (self.domains_dir / mode / "domain.toml").is_file()

    def domain(self, name: str) -> Domain:
        if name == "others" or not self._available(name):
            raise ModeUnavailable(name)
        return load_domain(self.domains_dir / name)

    def others_generate(self, context: str) -> dict[str, str]:
        """A first prompt from the User's context, generated locally; the User edits and confirms it.

        # shortcut: a template until #21 generates it with the local model.
        """
        context = _required_text(context, "context")
        prompt = (
            "From the Transcript, list the concrete actions agreed on, with who does them and by when.\n"
            f"Context from the User: {context}\n"
            "Leave a value empty when the Transcript does not say it; never invent one."
        )
        return {"prompt": prompt}

    def others_confirm(self, context: str, prompt: str) -> dict[str, str]:
        digest = prompt_digest(_required_text(context, "context"), _required_text(prompt, "prompt"))
        self._confirmed.add(digest)
        return {"digest": digest}

    # --- Google account (#45): connect and disconnect from the app

    def google_status(self) -> dict[str, Any]:
        return self.google.status() if self.google is not None else {"available": False, "connected": False}

    def google_connect(self) -> dict[str, Any]:
        if self.google is None:
            raise ModeUnavailable("google_calendar")
        return self.google.connect()

    def google_disconnect(self) -> dict[str, Any]:
        if self.google is None:
            raise ModeUnavailable("google_calendar")
        return self.google.disconnect()

    # --- Trello account (#72): the token the User pastes, the list work items go to

    def trello_status(self) -> dict[str, Any]:
        return self.trello.status() if self.trello is not None else {"available": False, "connected": False, "list": None}

    def _trello(self):
        if self.trello is None:
            raise ModeUnavailable("trello")
        return self.trello

    def trello_authorize(self) -> dict[str, Any]:
        return self._trello().authorize()

    def trello_connect(self, token: Any) -> dict[str, Any]:
        return self._trello().connect(token)

    def trello_boards(self) -> dict[str, Any]:
        return self._trello().boards()

    def trello_choose_list(self, list_id: Any) -> dict[str, Any]:
        return self._trello().choose_list(list_id)

    def trello_disconnect(self) -> dict[str, Any]:
        return self._trello().disconnect()

    # --- Jira (#73): connect, disconnect and the project for the work items sent to Jira

    def _jira(self):
        if self.jira is None:
            raise ModeUnavailable("jira")
        return self.jira

    def jira_status(self) -> dict[str, Any]:
        return self.jira.status() if self.jira is not None else {"available": False, "connected": False, "destination": None}

    def jira_connect(self) -> dict[str, Any]:
        return self._jira().connect()

    def jira_disconnect(self) -> dict[str, Any]:
        return self._jira().disconnect()

    def jira_projects(self) -> dict[str, Any]:
        return self._jira().projects()

    def jira_choose(self, cloud_id: str | None, project_key: str | None) -> dict[str, Any]:
        return self._jira().choose(cloud_id, project_key)

    def _destination_ready(self, connector: str) -> bool:
        """A destination the User can pick now (#72): local ones always, Trello once connected with a
        list, Jira once connected with a project (#73)."""
        if connector in LOCAL_CONNECTORS:
            return True
        if connector == "trello":
            return self.trello is not None and self.trello.ready()
        return connector == "jira" and self.jira is not None and self.jira.ready()

    # --- recording

    def start(self, mode: str, language: str, contact: str, others: dict[str, Any] | None = None) -> dict[str, str]:
        if mode not in {m for m, _, _ in MODES}:
            raise ModeUnavailable(str(mode))
        if mode == "others":
            others = others or {}
            digest = prompt_digest(str(others.get("context", "")), str(others.get("prompt", "")))
            if digest not in self._confirmed:
                raise PromptNotConfirmed("others")
        session = Session(id=f"s-{uuid.uuid4().hex[:10]}", started_at=self.clock.now(), domain=mode,
                          contact_id=str(contact or "") or None)
        self.store.save_session(session)
        # Others has no Domain to analyse with until #21; its live view replays the corporate meeting
        demo = self.demos["medical" if mode == "medical" else "corporate"]
        self._sessions[session.id] = {"demo": demo, "language": str(language), "started": self.monotonic(), "paused_at": None,
                                      "paused_total": 0.0, "stopped": False, "roles": {}, "summary": ""}
        return {"session_id": session.id}

    def live(self, session_id: str) -> dict[str, Any]:
        state = self._state(session_id)
        elapsed = self._elapsed(state)
        segments = state["demo"]["segments"]
        shown = min(len(segments), int(elapsed // SECONDS_PER_SEGMENT) + 1)
        return {
            "elapsed": int(elapsed),
            "paused": state["paused_at"] is not None,
            "segments": [{"speaker_id": s["speaker_id"], "text": s["text"]} for s in segments[:shown]],
        }

    def pause(self, session_id: str) -> dict[str, Any]:
        state = self._state(session_id)
        if state["paused_at"] is None:
            state["paused_at"] = self.monotonic()
        else:
            state["paused_total"] += self.monotonic() - state["paused_at"]
            state["paused_at"] = None
        return {"paused": state["paused_at"] is not None}

    def stop(self, session_id: str) -> dict[str, Any]:
        """The authoritative Transcript, then the analysis. Runs once; a second stop changes nothing."""
        state = self._state(session_id)
        if state["stopped"]:
            return {"session_id": session_id}
        session = self.store.get_session(session_id)
        domain = self.domain(session.domain)
        demo = state["demo"]
        segments = tuple(Segment(float(i), float(i + 1), s["text"], s["speaker_id"])
                         for i, s in enumerate(demo["segments"]))
        session.transcript_ref = self.store.save_transcript(session_id, Transcript(segments, demo["language"]))
        self.store.save_session(session)
        actions = [ProposedAction(id=f"{session_id}-{n}", **_with_date(raw)) for n, raw in enumerate(demo["actions"], 1)]
        suggestions = [Suggestion(**s) for s in demo["suggestions"]]
        revision = self.store.load_actions_snapshot(session_id).revision
        self.store.save_actions(session_id, actions, expected_revision=revision, suggestions=suggestions)
        state.update(stopped=True, roles=dict(demo["speakers"]), summary=demo["summary"])
        return {"session_id": session_id}

    def import_example(self, mode: str, language: str, contact: str, others: dict[str, Any] | None = None) -> dict[str, str]:
        session_id = self.start(mode, language, contact, others)["session_id"]
        return self.stop(session_id)

    def import_file(self, mode: str, language: str, contact: str, others: dict[str, Any] | None = None) -> dict[str, str]:
        return self.import_example(mode, language, contact, others)  # the demo has its examples, no file dialog

    def progress(self, session_id: str) -> dict[str, Any]:
        self._state(session_id)
        return {"stage": "ready", "failed": None}  # stop() analysed it already

    # --- review

    def sessions(self) -> list[dict[str, Any]]:
        listed = []
        for session_id, state in self._sessions.items():
            session = self.store.get_session(session_id)
            if session is None or not state["stopped"]:
                continue
            actions = self.store.load_actions(session_id)
            listed.append({
                "session_id": session_id, "contact": session.contact_id or "", "domain": session.domain,
                "started_at": session.started_at.isoformat(),
                "pending": sum(a.status is ActionStatus.PROPOSED for a in actions), "ready": True,
            })
        return sorted(listed, key=lambda s: s["started_at"], reverse=True)

    def review(self, session_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise LookupError("Session does not exist")
        domain = self.domain(session.domain)
        snapshot = self.store.load_actions_snapshot(session_id)
        transcript = self.store.load_transcript(session.transcript_ref)
        # the spoken Transcript (#82); before translation, or for an English Session, it is the same one
        spoken_ref = session.source_transcript_ref or session.transcript_ref
        spoken = transcript if spoken_ref == session.transcript_ref else self.store.load_transcript(spoken_ref)
        return {
            "session_id": session_id,
            "revision": snapshot.revision,
            "contact": session.contact_id or "",
            "started_at": session.started_at.isoformat(),
            "domain": {"name": domain.name, "label": domain.label},
            "summary": self._summary(session_id),
            "speakers": self._speakers(session_id, transcript),
            "untranslated": self._partial(session_id, transcript),
            "transcript": self._transcript_view(spoken, transcript if spoken_ref != session.transcript_ref else None),
            # a correction dropped the translation (#82): the actions come from the old text until regenerated
            # shortcut: an English Session keeps no trace of a correction, so after a restart only a Role change
            # brings the button back; store a marker if that shows up in practice.
            "retranslate": bool(session.source_transcript_ref) and spoken_ref == session.transcript_ref
                           and spoken.language != "en",
            "can_execute": self.can_execute,
            "roles": [{"name": r.name, "label": r.label or r.name} for r in domain.roles],
            "actions": [self._action_view(a, domain) for a in snapshot.actions],
            "suggestions": [dataclasses.asdict(s) for s in self.store.load_suggestions(session_id)],
        }

    def _summary(self, session_id: str) -> str:
        return self._state(session_id)["summary"]

    def _transcript_view(self, spoken: Transcript, translated: Transcript | None) -> dict[str, Any]:
        """The final Transcript, made after Stop from the whole audio, with Speakers: as spoken (`index` is what
        edit_transcript corrects, #82), and the English one the analysis read when it was translated."""
        lines = lambda t: [{"index": i, "speaker_id": s.speaker_id, "start": s.start, "end": s.end, "text": s.text,
                            "untranslated": s.untranslated} for i, s in enumerate(t.segments)]
        return {"spoken": lines(spoken), "spoken_language": spoken.language,
                "translated": lines(translated) if translated else [],
                "translated_language": translated.language if translated else None}

    def _speakers(self, session_id: str, transcript: Transcript) -> list[dict[str, Any]]:
        roles = self._state(session_id)["roles"]
        return [{"id": sid, "role": roles.get(sid)} for sid in _speaker_ids(transcript)]

    def _partial(self, session_id: str, transcript: Transcript) -> int:
        return 0  # the demo Transcript is complete

    def _action_view(self, action: ProposedAction, domain: Domain) -> dict[str, Any]:
        action_type = domain.action_type(action.action_type)
        fields = [{
            "name": f.name, "label": f.label or f.name, "type": f.type, "required": f.required,
            "value": action.fields.get(f.name) or "",
            "missing": f.required and not (action.fields.get(f.name) or "").strip(),
        } for f in action_type.fields] if action_type else []
        plan = action.approved_payload if action.status is not ActionStatus.PROPOSED else action.prepared_payload
        return {
            "id": action.id,
            "title": action.title,
            "description": action.description,
            "transcript_excerpt": action.transcript_excerpt,
            "owner": action.owner or "",
            "due_date": action.due_date.isoformat() if action.due_date else "",
            "action_type": action.action_type,
            "type_label": action_type.label if action_type else "",
            "status": action.status.value,
            "fields": fields,
            # an empty title is refused only at preview ('required'), so it is shown missing like a required field
            "missing": ([] if action.title.strip() else ["Titlu"]) + [f["label"] for f in fields if f["missing"]],
            "plan": [{"step": s["step"], "connector": s["connector"],
                      "connector_label": CONNECTOR_LABELS.get(s["connector"], s["connector"]),
                      "payload": s["payload"],
                      # an email draft is also shown decoded (To, Subject, body) above the exact file (#61)
                      "readable": email_preview(s["payload"].get("content")) if s["connector"] == "email_draft" else None,
                      } for s in (plan or {}).get("steps", [])],
            "prepared": action.prepared_payload is not None,
            "destinations": self._destinations(action_type),
        }

    def delete(self, session_id: str) -> dict[str, Any]:
        """The User's confirmed deletion (#59); the audit log of executed actions is kept."""
        try:
            self.store.delete_session(session_id)
        except ValueError:  # an approved action waits to execute; LookupError (unknown Session) passes through
            raise ExecutionPending("session") from None
        self._sessions.pop(session_id, None)
        return {"deleted": session_id}

    def regenerate(self, session_id: str) -> dict[str, Any]:
        raise ModeUnavailable("regenerate")  # the demo has no model to analyse again

    def set_role(self, session_id: str, speaker_id: str, role: str) -> dict[str, Any]:
        state = self._state(session_id)
        domain = self.domain(self.store.get_session(session_id).domain)
        if role not in {r.name for r in domain.roles}:
            raise ValueError("unknown role")
        state["roles"][str(speaker_id)] = role
        return self.review(session_id)

    def edit_transcript(self, session_id: str, index: int, text: str) -> dict[str, Any]:
        """The User's correction of one spoken Segment (#82); actions change only on "Regenerează"."""
        correct_transcript(session_id, index, text, self.store)
        return self.review(session_id)

    def edit(self, session_id: str, action_id: str, changes: dict[str, Any], revision: int) -> dict[str, Any]:
        domain = self.domain(self.store.get_session(session_id).domain)
        edit_action(session_id, action_id, changes, expected_revision=revision, store=self.store, domain=domain)
        return self.review(session_id)

    def preview(self, session_id: str, action_id: str, revision: int, destination: str | None = None) -> dict[str, Any]:
        """`destination`: the Connector the User picked for the action (#72), among the ones ready now."""
        domain = self.domain(self.store.get_session(session_id).domain)
        options = {}
        if destination is not None:
            action = next((a for a in self.store.load_actions(session_id) if a.id == action_id), None)
            action_type = domain.action_type(action.action_type) if action is not None else None
            ready = [d["connector"] for d in self._destinations(action_type) if d["ready"]]
            if destination not in ready:
                raise ValidationError("unknown_destination", "destination")
            options = {"destination": destination}
        preview_action(session_id, action_id, options, expected_revision=revision, store=self.store,
                       domain=domain, builder=self.builder)
        return self.review(session_id)

    def _destinations(self, action_type) -> list[dict[str, Any]]:
        """Where the User may send this Action Type (#72); empty when there is no choice."""
        steps = [s for s in action_type.workflow.steps if s.alternatives] if action_type else []
        if not steps:
            return []
        return [{"connector": c, "label": CONNECTOR_LABELS.get(c, c), "ready": self._destination_ready(c)}
                for c in steps[0].connectors()]

    def approve(self, session_id: str, action_id: str, revision: int) -> dict[str, Any]:
        domain = self.domain(self.store.get_session(session_id).domain)
        approve_action(session_id, action_id, expected_revision=revision, store=self.store, domain=domain,
                       clock=self.clock, approved_by=self.approved_by)
        return self.review(session_id)

    def reject(self, session_id: str, action_id: str, revision: int) -> dict[str, Any]:
        reject_action(session_id, action_id, expected_revision=revision, store=self.store)
        return self.review(session_id)

    # ---

    def _state(self, session_id: str) -> dict[str, Any]:
        if session_id not in self._sessions:
            raise LookupError("Session does not exist")
        return self._sessions[session_id]

    def _elapsed(self, state: dict[str, Any]) -> float:
        end = state["paused_at"] if state["paused_at"] is not None else self.monotonic()
        return max(0.0, end - state["started"] - state["paused_total"])


def _with_date(raw: dict[str, Any]) -> dict[str, Any]:
    raw = dict(raw)
    raw["due_date"] = date.fromisoformat(raw["due_date"]) if raw.get("due_date") else None
    return raw


def _speaker_ids(transcript: Transcript) -> list[str]:
    seen: list[str] = []
    for segment in transcript.segments:
        if segment.speaker_id and segment.speaker_id not in seen:
            seen.append(segment.speaker_id)
    return seen


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value
