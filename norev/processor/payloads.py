"""PayloadBuilders (core/ports, #18) for the `local_document` (#23), `google_calendar` (#44) and
`email_draft` (#61) Connectors.

It makes, at preview time, the final payload of each `local_document` step: the exact document text
and the path it will be written to, so the User approves both (ADR 0002). The folder is the app's,
given when the builder is built, and the file name comes from the content: no option, Domain or
model answer chooses the path. LocalDocumentConnector.send() checks the same rule again.
"""
import dataclasses
import os
import re
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from norev.adapters.connectors import email_draft, trello
from norev.adapters.connectors.local_document import NAME, destination, export_dir_of, render
from norev.core.models import Domain, ProposedAction, UnsupportedWorkflow, ValidationError, Workflow
from norev.core.ports import PayloadBuilder
from norev.usecases.approve import domain_digest

GENERIC_FIELDS = ("title", "description", "owner", "due_date", "transcript_excerpt")  # domains/README.md
MAX_CONTENT = 100_000  # characters; a longer edited document is refused


def _value(action: ProposedAction, name: str) -> Any:
    if name in GENERIC_FIELDS:
        value = getattr(action, name)
        return value.isoformat() if isinstance(value, date) else value
    return action.fields.get(name)


class LocalDocumentPayloadBuilder:
    """For Workflows whose Connector steps are all `local_document`.

    options: `{}`, or `{"<step name>": {"content": "<the User's edited text>"}}` to replace the
    rendered document. Any other option, or a step with another Connector, is refused.
    """

    def __init__(self, export_dir: Path) -> None:
        self.export_dir = export_dir_of(export_dir)

    def build(self, action: ProposedAction, domain: Domain, options: dict[str, Any]) -> dict[str, Any]:
        action_type = domain.action_type(action.action_type)
        if action_type is None:
            raise ValidationError("unknown_action_type", "action_type")
        connector_steps = [s for s in action_type.workflow.steps if s.connector]
        for step in connector_steps:
            if step.connector != NAME:
                raise UnsupportedWorkflow(f"no payload builder for Connector {step.connector!r}")
        if set(options) - {s.name for s in connector_steps}:
            raise ValidationError("unknown_options", "options")

        steps = []
        for step in connector_steps:
            option = options.get(step.name, {})
            if not isinstance(option, dict) or set(option) - {"content"}:
                raise ValidationError("invalid_options", step.name)
            content = option.get("content")
            if content is None:
                content = render({name: _value(action, name) for name in step.sends})
            elif not isinstance(content, str) or not content.strip() or len(content) > MAX_CONTENT:
                raise ValidationError("invalid_content", step.name)
            payload = {"destination": str(destination(self.export_dir, content)), "content": content}
            steps.append({"step": step.name, "connector": NAME, "payload": payload})
        return {"version": 1, "domain": domain.name, "domain_digest": domain_digest(domain),
                "action_type": action.action_type, "steps": steps}


CALENDAR = "google_calendar"
DEFAULT_TIME_ZONE = "Europe/Bucharest"
_EMAIL = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")


def _zone_time(day: date, at: time, zone: ZoneInfo, field: str) -> str:
    local = datetime.combine(day, at, tzinfo=zone)  # fold=0: in the repeated autumn hour, the first one
    if local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
        raise ValidationError("nonexistent_time", field)  # skipped by the spring clock change
    return local.isoformat()


def _attendees(value: str | None) -> list[dict[str, str]]:
    emails: list[str] = []
    for part in (value or "").split(","):
        email = part.strip()
        if not email:
            continue
        if not _EMAIL.fullmatch(email):
            raise ValidationError("invalid_email", "attendees")
        if email.lower() not in {e.lower() for e in emails}:
            emails.append(email)
    return [{"email": e} for e in emails]


class GoogleCalendarPayloadBuilder:
    """For Workflows whose Connector steps are all `google_calendar` (#44); #45 sends the payload.

    Each step's payload is the exact `events.insert` request: `{"calendar_id", "event"}`. From the
    step's `sends`: `title` is the summary, the one `date` field the day, the two `time` fields the
    start and end, `location` and `attendees` (emails the User typed, comma-separated) map to theirs;
    `description` and any other text field go into the description. The time zone is explicit in
    every dateTime (offset) and in `timeZone`. `event.id` is made here, so a retry of the approved
    payload cannot create a second event. options must be `{}`.
    """

    def __init__(self, time_zone: str = DEFAULT_TIME_ZONE, *, calendar_id: str = "primary",
                 new_id: Callable[[], str] = lambda: uuid.uuid4().hex) -> None:
        self.zone = ZoneInfo(time_zone)  # an unknown zone fails here, not at preview
        self.calendar_id = calendar_id
        self.new_id = new_id

    @classmethod
    def from_environment(cls) -> "GoogleCalendarPayloadBuilder":
        return cls(os.environ.get("NOREV_TIMEZONE") or DEFAULT_TIME_ZONE)

    def build(self, action: ProposedAction, domain: Domain, options: dict[str, Any]) -> dict[str, Any]:
        action_type = domain.action_type(action.action_type)
        if action_type is None:
            raise ValidationError("unknown_action_type", "action_type")
        if options:
            raise ValidationError("unknown_options", "options")
        connector_steps = [s for s in action_type.workflow.steps if s.connector]
        for step in connector_steps:
            if step.connector != CALENDAR:
                raise UnsupportedWorkflow(f"no payload builder for Connector {step.connector!r}")
        labels = {f.name: f.label for f in action_type.fields}
        types = {f.name: f.type for f in action_type.fields}
        steps = []
        for step in connector_steps:
            if set(step.sends) - set(types) - {"title", "description"}:
                # owner, due_date, transcript_excerpt have no place in an event: refuse, never drop silently
                raise UnsupportedWorkflow(f"step {step.name!r} sends fields an event cannot hold")
            dates = [n for n in step.sends if types.get(n) == "date"]
            times = [n for n in step.sends if types.get(n) == "time"]
            if "title" not in step.sends or len(dates) != 1 or len(times) != 2:
                # shortcut: one-off events only; recurring ones (medication_reminder) are out of scope for #44
                raise UnsupportedWorkflow(f"step {step.name!r} is not a single event")
            for name in dates + times:
                if not _value(action, name):
                    raise ValidationError("required", name)
            day = date.fromisoformat(_value(action, dates[0]))
            start, end = (time.fromisoformat(_value(action, n)) for n in times)
            if end <= start:
                raise ValidationError("end_before_start", times[1])
            notes = [_value(action, "description")] if "description" in step.sends else []
            notes += [f"{labels[n]}: {_value(action, n)}" for n in step.sends
                      if types.get(n) == "string" and n not in ("location", "attendees") and _value(action, n)]
            event: dict[str, Any] = {
                "id": self.new_id(),
                "summary": action.title,
                "description": "\n".join(n for n in notes if n),
                "start": {"dateTime": _zone_time(day, start, self.zone, times[0]), "timeZone": self.zone.key},
                "end": {"dateTime": _zone_time(day, end, self.zone, times[1]), "timeZone": self.zone.key},
            }
            if "location" in step.sends and _value(action, "location"):
                event["location"] = _value(action, "location")
            attendees = _attendees(_value(action, "attendees")) if "attendees" in step.sends else []
            if attendees:
                event["attendees"] = attendees
            steps.append({"step": step.name, "connector": CALENDAR,
                          "payload": {"calendar_id": self.calendar_id, "event": event}})
        return {"version": 1, "domain": domain.name, "domain_digest": domain_digest(domain),
                "action_type": action.action_type, "steps": steps}


class ConnectorPayloadBuilder:
    """Picks the builder for the Connector of an Action Type's Workflow; one Connector per Workflow.

    `options["destination"]` (#72): the Connector the User chose instead of the step's default; it
    must be one of the step's `alternatives`. The chosen builder sees the step with that Connector,
    and the plan keeps the real Domain's digest, so approval and the executor check it as usual.
    The other options go to the chosen builder.
    """

    def __init__(self, builders: dict[str, PayloadBuilder]) -> None:
        self.builders = builders

    def build(self, action: ProposedAction, domain: Domain, options: dict[str, Any]) -> dict[str, Any]:
        action_type = domain.action_type(action.action_type)
        if action_type is None:
            raise ValidationError("unknown_action_type", "action_type")
        steps = [s for s in action_type.workflow.steps if s.connector]
        defaults = {s.connector for s in steps}
        if len(defaults) != 1:
            raise UnsupportedWorkflow(f"no payload builder for Connectors {sorted(defaults)!r}")
        options = dict(options)
        destination = options.pop("destination", None)
        chosen = steps[0].connector if destination is None else destination
        if not isinstance(chosen, str) or any(chosen not in s.connectors() for s in steps):
            raise ValidationError("unknown_destination", "destination")
        if chosen not in self.builders:
            raise UnsupportedWorkflow(f"no payload builder for Connector {chosen!r}")
        if chosen == steps[0].connector:
            return self.builders[chosen].build(action, domain, options)
        swapped = dataclasses.replace(action_type, workflow=Workflow(tuple(
            dataclasses.replace(s, connector=chosen, alternatives=()) if s.connector else s
            for s in action_type.workflow.steps)))
        view = dataclasses.replace(domain, action_types=tuple(
            swapped if a.name == action_type.name else a for a in domain.action_types))
        plan = self.builders[chosen].build(action, view, options)
        if isinstance(plan, dict):
            plan["domain_digest"] = domain_digest(domain)  # the plan is checked against the real Domain
        return plan


EMAIL_FIELDS = ("recipients", "subject", "body")  # what an email_draft step sends (domains/corporate)


class EmailDraftPayloadBuilder:
    """For Workflows whose Connector steps are all `email_draft` (#61).

    Each step's payload is the exact draft file and its path, `{"destination", "content"}`, made from
    the action's `recipients` (emails the User typed), `subject` and `body`. The folder is the app's
    and the file name comes from the content. options must be `{}`: the User edits the fields.
    """

    def __init__(self, export_dir: Path) -> None:
        self.export_dir = export_dir_of(export_dir)

    def build(self, action: ProposedAction, domain: Domain, options: dict[str, Any]) -> dict[str, Any]:
        action_type = domain.action_type(action.action_type)
        if action_type is None:
            raise ValidationError("unknown_action_type", "action_type")
        if options:
            raise ValidationError("unknown_options", "options")
        connector_steps = [s for s in action_type.workflow.steps if s.connector]
        for step in connector_steps:
            if step.connector != email_draft.NAME:
                raise UnsupportedWorkflow(f"no payload builder for Connector {step.connector!r}")
            if set(step.sends) != set(EMAIL_FIELDS):
                raise UnsupportedWorkflow(f"step {step.name!r} must send exactly {', '.join(EMAIL_FIELDS)}")
        steps = []
        for step in connector_steps:
            content = email_draft.build_eml(*(action.fields.get(name) for name in EMAIL_FIELDS))
            payload = {"destination": str(email_draft.destination(self.export_dir, content)), "content": content}
            steps.append({"step": step.name, "connector": email_draft.NAME, "payload": payload})
        return {"version": 1, "domain": domain.name, "domain_digest": domain_digest(domain),
                "action_type": action.action_type, "steps": steps}


def email_preview(content: Any) -> dict[str, str] | None:
    """To, Subject and body of an email_draft payload, decoded for the preview next to the exact file
    (#61); None if it is not a draft made by norev."""
    try:
        return email_draft.read_eml(content)
    except ValueError:
        return None


class TrelloPayloadBuilder:
    """For a work item sent to Trello (#72): the exact card and the list it goes to.

    `chosen_list()` gives the list the User chose ({"id", "name"}), None before they chose one.
    The card: `name` from the title; `desc` with the description, the owner as text (no Trello member
    is ever assigned: a Speaker is not a Contact), the step's other text fields with their labels, the
    Transcript quote, and a last line `norev:<id>` that lets a Retry find the card instead of making a
    second one; `due` is the due date at 12:00 in the time zone, with its offset. options must be `{}`.
    """

    def __init__(self, chosen_list: Callable[[], dict[str, str] | None], time_zone: str = DEFAULT_TIME_ZONE, *,
                 new_id: Callable[[], str] = lambda: uuid.uuid4().hex) -> None:
        self.chosen_list = chosen_list
        self.zone = ZoneInfo(time_zone)
        self.new_id = new_id

    @classmethod
    def from_environment(cls, chosen_list: Callable[[], dict[str, str] | None]) -> "TrelloPayloadBuilder":
        return cls(chosen_list, os.environ.get("NOREV_TIMEZONE") or DEFAULT_TIME_ZONE)

    def build(self, action: ProposedAction, domain: Domain, options: dict[str, Any]) -> dict[str, Any]:
        action_type = domain.action_type(action.action_type)
        if action_type is None:
            raise ValidationError("unknown_action_type", "action_type")
        if options:
            raise ValidationError("unknown_options", "options")
        connector_steps = [s for s in action_type.workflow.steps if s.connector]
        for step in connector_steps:
            if step.connector != trello.NAME:
                raise UnsupportedWorkflow(f"no payload builder for Connector {step.connector!r}")
        target = self.chosen_list()
        if not target:
            raise ValidationError("no_trello_list", "destination")
        labels = {f.name: f.label or f.name for f in action_type.fields}
        steps = []
        for step in connector_steps:
            if not action.title.strip():
                raise ValidationError("required", "title")
            lines = [action.description.strip()] if "description" in step.sends and action.description.strip() else []
            if "owner" in step.sends and action.owner:
                lines.append(f"Responsabil: {action.owner}")
            lines += [f"{labels[n]}: {action.fields[n]}" for n in step.sends if n in labels and action.fields.get(n)]
            if "transcript_excerpt" in step.sends and action.transcript_excerpt.strip():
                lines.append(f"Din conversație: „{action.transcript_excerpt.strip()}”")
            marker = f"norev:{self.new_id()}"
            card: dict[str, Any] = {"name": action.title.strip(), "desc": "\n\n".join(lines + [marker])}
            if "due_date" in step.sends and action.due_date:
                card["due"] = datetime.combine(action.due_date, time(12, 0), tzinfo=self.zone).isoformat()
            steps.append({"step": step.name, "connector": trello.NAME,
                          "payload": {"list": {"id": target["id"], "name": target["name"]}, "card": card}})
        return {"version": 1, "domain": domain.name, "domain_digest": domain_digest(domain),
                "action_type": action.action_type, "steps": steps}
