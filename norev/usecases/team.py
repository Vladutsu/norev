"""The one local Team (ADR 0006): who founds it, who joins it, who changes what.

Only the rules and the checks live here; the key slots that make a password or an Invitation code
open the store are the sign-in gate's job (norev/ui/accounts.py). Every value from the UI is checked:
a refusal is a ValidationError with a fixed code, never the value itself.
"""
import dataclasses
import re
import secrets
from datetime import datetime, timedelta

from norev.core.models import Invitation, Member, Team, ValidationError
from norev.core.ports import TeamStore

INVITATION_DAYS = 7
MAX_SKILLS, MAX_SKILL, MAX_NAME, MAX_EMAIL = 30, 40, 80, 254
MIN_PASSWORD, MAX_PASSWORD = 8, 200
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I: read aloud or copied by hand


class NotTeamLeader(Exception):
    """Only the Team Leader may do this (ADR 0006)."""


def check_email(value: object) -> str:
    email = value.strip().lower() if isinstance(value, str) else ""
    if len(email) > MAX_EMAIL or not _EMAIL.fullmatch(email):
        raise ValidationError("invalid_email", "email")
    return email


def check_name(value: object, field: str = "name") -> str:
    name = " ".join(value.split()) if isinstance(value, str) else ""
    if not name or len(name) > MAX_NAME or not name.isprintable():
        raise ValidationError("invalid_name", field)
    return name


def check_password(value: object) -> str:
    if not isinstance(value, str) or not MIN_PASSWORD <= len(value) <= MAX_PASSWORD:
        raise ValidationError("weak_password", "password")
    return value


def check_skills(value: object) -> tuple[str, ...]:
    """A list, or one text with commas; empty ones dropped, duplicates (any case) kept once."""
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)) or not all(isinstance(s, str) for s in items):
        raise ValidationError("invalid_skills", "skills")
    skills: list[str] = []
    for item in (" ".join(s.split()) for s in items):
        if not item:
            continue
        if len(item) > MAX_SKILL or not item.isprintable():
            raise ValidationError("invalid_skills", "skills")
        if item.lower() not in (s.lower() for s in skills):
            skills.append(item)
    if len(skills) > MAX_SKILLS:
        raise ValidationError("too_many_skills", "skills")
    return tuple(skills)


def new_code() -> str:
    """An Invitation code: 12 characters (~60 bits) in three groups, e.g. K7QF-2MXP-9TRA."""
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))
    return "-".join(raw[i:i + 4] for i in range(0, 12, 4))


def normal_code(value: object) -> str:
    """What the Member typed, as the code was made: upper case, dashes and spaces ignored."""
    raw = re.sub(r"[\s-]", "", value.upper()) if isinstance(value, str) else ""
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def found_team(store: TeamStore, email: object, name: object, skills: object) -> Member:
    """The first User: the Team Leader of a new Team."""
    if store.list_members():
        raise ValueError("the Team already exists")
    leader = Member(check_email(email), check_name(name), check_skills(skills), leader=True)
    if store.get_team() is None:
        store.save_team(Team("Echipa"))
    store.save_member(leader)
    return leader


def leader_of(store: TeamStore, email: str) -> Member:
    member = store.get_member(email)
    if member is None or not member.leader:
        raise NotTeamLeader()
    return member


def invite(store: TeamStore, by: str, email: object, now: datetime) -> Invitation:
    leader_of(store, by)
    email = check_email(email)
    if store.get_member(email) is not None:
        raise ValidationError("already_member", "email")
    invitation = Invitation(email, now + timedelta(days=INVITATION_DAYS))
    store.save_invitation(invitation)
    return invitation


def invitation_for(store: TeamStore, email: str, now: datetime) -> Invitation:
    invitation = next((i for i in store.list_invitations() if i.email == email), None)
    if invitation is None:
        raise ValidationError("no_invitation", "email")
    if now >= invitation.expires_at:
        raise ValidationError("invitation_expired", "code")
    return invitation


def join(store: TeamStore, email: object, name: object, skills: object, now: datetime) -> Member:
    """An invited Member signs up: they join the Team and the Invitation is used up."""
    email = check_email(email)
    invitation_for(store, email, now)
    member = Member(email, check_name(name), check_skills(skills))
    store.save_member(member)
    store.delete_invitation(email)
    return member


def cancel_invitation(store: TeamStore, by: str, email: object) -> None:
    leader_of(store, by)
    store.delete_invitation(check_email(email))


def remove_member(store: TeamStore, by: str, email: object) -> None:
    leader_of(store, by)
    email = check_email(email)
    if email == by:
        raise ValidationError("cannot_remove_self", "email")
    if store.get_member(email) is None:
        raise LookupError("no such Member")
    store.delete_member(email)


def rename_team(store: TeamStore, by: str, name: object) -> Team:
    leader_of(store, by)
    team = Team(check_name(name, "team"))
    store.save_team(team)
    return team


def update_profile(store: TeamStore, email: str, *, name: object, skills: object) -> Member:
    """A Member's own name and Skills."""
    member = _member(store, email)
    member = dataclasses.replace(member, name=check_name(name), skills=check_skills(skills))
    store.save_member(member)
    return member


def link(store: TeamStore, email: str, service: str, user_id: str | None, user_name: str = "") -> Member:
    """The Member's own Jira or Trello user, as that service named it when they signed in there
    (never typed); None unlinks it. Only this id is ever sent as an assignee."""
    member = _member(store, email)
    if service == "jira":
        member = dataclasses.replace(member, jira_account_id=user_id, jira_user=user_name if user_id else "")
    elif service == "trello":
        member = dataclasses.replace(member, trello_member_id=user_id, trello_user=user_name if user_id else "")
    else:
        raise ValidationError("unknown_service", "service")
    store.save_member(member)
    return member


def _member(store: TeamStore, email: str) -> Member:
    member = store.get_member(email)
    if member is None:
        raise LookupError("no such Member")
    return member
