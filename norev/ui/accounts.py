"""The sign-in gate of the desktop app (ADR 0006): nothing opens the store but a User's password.

The Bridge talks to a Gate. Before sign-in it answers only auth_status, sign_up and sign_in; every
other call is refused with NotSignedIn. Signing in opens the store with the data key from the User's
key slot and builds the real backend for that User (`build(store, member)`), to which every other call
then goes. The Gate also keeps the Team: Invitations (a key slot under a one-time code), Members, the
Team Leader's rights (connecting Jira, Trello and Google, picking who takes a Proposed Action).
"""
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from norev.adapters.store.encrypted import KEY_BYTES, EncryptedStore, StoreKeyError, read_key_file
from norev.adapters.store.slots import InvitationExpired, KeySlots, SignInFailed
from norev.core.models import Member, ValidationError
from norev.usecases import team

# Connecting an account the whole Team sends through is the Team Leader's (ADR 0006)
LEADER_ONLY = frozenset({
    "google_connect", "google_disconnect", "trello_authorize", "trello_connect", "trello_sign_in", "trello_set_key", "trello_choose_list",
    "trello_disconnect", "jira_connect", "jira_disconnect", "jira_choose",
})


class NotSignedIn(Exception):
    """No User is signed in: only auth_status, sign_up and sign_in answer."""


class SignedInElsewhere(Exception):
    """A Session records or is analysed: signing out now would close the store under it."""


def _now() -> datetime:
    return datetime.now().astimezone()


def member_view(m: Member) -> dict[str, Any]:
    return {"email": m.email, "name": m.name, "skills": list(m.skills), "leader": m.leader,
            "jira_user": m.jira_user if m.jira_account_id else "", "trello_user": m.trello_user if m.trello_member_id else "",
            "trello_id": m.trello_member_id}


class Gate:
    def __init__(self, *, store_path: Path, slots: KeySlots, build: Callable[[EncryptedStore, Member], Any],
                 legacy_key_file: Path | None = None, now: Callable[[], datetime] = _now) -> None:
        self.store_path = Path(store_path)
        self.slots = slots
        self.build = build
        self.legacy_key_file = Path(legacy_key_file) if legacy_key_file else None
        self.now = now
        self._lock = threading.RLock()
        self._store: EncryptedStore | None = None
        self._key: bytes | None = None
        self._member: Member | None = None
        self._backend: Any = None

    def __repr__(self) -> str:
        return f"Gate(signed_in={self._member is not None})"

    # --- before sign-in

    def auth_status(self) -> dict[str, Any]:
        legacy = self.legacy_key_file is not None and self.legacy_key_file.is_file()
        first = self.slots.empty()
        return {
            "accounts": True,
            "signed_in": self._member is not None,
            "first_run": first,
            # a store from before ADR 0006 with no key to open it: nobody can sign up into it
            "locked_out": first and self.store_path.exists() and not legacy,
            "legacy_key": legacy,
            "user": member_view(self._member) if self._member else None,
        }

    def sign_up(self, email: Any, name: Any, password: Any, skills: Any, code: Any = "") -> dict[str, Any]:
        with self._lock:
            if self._member is not None:
                raise ValueError("already signed in")
            email, name = team.check_email(email), team.check_name(name)
            password, skills = team.check_password(password), team.check_skills(skills)
            if self.slots.empty():
                key = self._first_key()
                store = EncryptedStore(self.store_path, key)  # creates it, or checks the old key opens it
                try:
                    self.slots.add("user", email, password, key)
                    member = team.found_team(store, email, name, skills)
                except Exception:
                    self.slots.remove("user", email)
                    store.close()
                    raise
            else:
                if self.slots.has("user", email):
                    raise ValidationError("already_member", "email")
                try:
                    key = self.slots.unlock("invitation", email, team.normal_code(code), now=self.now())
                except SignInFailed:
                    raise ValidationError("invalid_code", "code") from None
                except InvitationExpired:
                    raise ValidationError("invitation_expired", "code") from None
                store = EncryptedStore(self.store_path, key)
                try:
                    self.slots.add("user", email, password, key)
                    member = team.join(store, email, name, skills, self.now())
                except Exception:
                    self.slots.remove("user", email)
                    store.close()
                    raise
                self.slots.remove("invitation", email)
            self._open(store, key, member)
            return self.auth_status()

    def _first_key(self) -> bytes:
        if self.legacy_key_file is not None and self.legacy_key_file.is_file():
            return read_key_file(self.legacy_key_file)
        if self.store_path.exists():
            raise ValidationError("store_without_key", "store")
        return os.urandom(KEY_BYTES)

    def sign_in(self, email: Any, password: Any) -> dict[str, Any]:
        with self._lock:
            if self._member is not None:
                raise ValueError("already signed in")
            email = email.strip().lower() if isinstance(email, str) else ""
            try:
                key = self.slots.unlock("user", email, password if isinstance(password, str) else "")
                store = EncryptedStore(self.store_path, key)
            except (SignInFailed, StoreKeyError):
                raise ValidationError("sign_in_failed", "password") from None
            member = store.get_member(email)
            if member is None:  # a slot left by a Member who was removed
                store.close()
                self.slots.remove("user", email)
                raise ValidationError("sign_in_failed", "password")
            self._open(store, key, member)
            return self.auth_status()

    def _open(self, store: EncryptedStore, key: bytes, member: Member) -> None:
        try:
            backend = self.build(store, member)
        except Exception:
            store.close()
            raise
        self._store, self._key, self._member, self._backend = store, key, member, backend

    def sign_out(self) -> dict[str, Any]:
        with self._lock:
            if self._backend is not None and getattr(self._backend, "busy", lambda: False)():
                raise SignedInElsewhere()
            if self._store is not None:
                self._store.close()
            self._store = self._key = self._member = self._backend = None
            return self.auth_status()

    # --- the Team (signed in)

    def _me(self) -> Member:
        if self._member is None:
            raise NotSignedIn()
        member = self._store.get_member(self._member.email)
        if member is None:  # removed by the Team Leader meanwhile
            self.sign_out()
            raise NotSignedIn()
        self._member = member
        return member

    def team(self) -> dict[str, Any]:
        me = self._me()
        t = self._store.get_team()
        return {"name": t.name if t else "", "me": me.email, "leader": me.leader,
                "members": [member_view(m) for m in self._store.list_members()],
                "invitations": [{"email": i.email, "expires_at": i.expires_at.isoformat(),
                                 "expired": self.now() >= i.expires_at}
                                for i in self._store.list_invitations()] if me.leader else []}

    def team_invite(self, email: Any) -> dict[str, Any]:
        with self._lock:
            me = self._me()
            invitation = team.invite(self._store, me.email, email, self.now())
            code = team.new_code()
            self.slots.add("invitation", invitation.email, code, self._key, expires=invitation.expires_at)
            return {"code": code, "email": invitation.email, "expires_at": invitation.expires_at.isoformat(), **self.team()}

    def team_cancel_invitation(self, email: Any) -> dict[str, Any]:
        with self._lock:
            team.cancel_invitation(self._store, self._me().email, email)
            self.slots.remove("invitation", team.check_email(email))
            return self.team()

    def team_remove(self, email: Any) -> dict[str, Any]:
        with self._lock:
            team.remove_member(self._store, self._me().email, email)
            self.slots.remove("user", team.check_email(email))
            return self.team()

    def team_rename(self, name: Any) -> dict[str, Any]:
        team.rename_team(self._store, self._me().email, name)
        return self.team()

    def profile_save(self, name: Any, skills: Any) -> dict[str, Any]:
        team.update_profile(self._store, self._me().email, name=name, skills=skills)
        self._me()
        return self.team()

    # A Member's own Jira and Trello users come from signing in there once (ADR 0006), never typed:
    # the Team still sends through the Team Leader's connection, with that user as the assignee.

    def profile_jira_connect(self) -> dict[str, Any]:
        me = self._me()
        found = self._backend.jira.identify()
        if not found["failed"]:
            team.link(self._store, me.email, "jira", found["account_id"], found["name"])
        return {**self.team(), "failed": found["failed"]}

    def profile_trello_connect(self) -> dict[str, Any]:
        me = self._me()
        found = self._backend.trello.identify_in_browser()
        if not found["failed"]:
            team.link(self._store, me.email, "trello", found["member_id"], found["username"])
        return {**self.team(), "failed": found["failed"]}

    def team_trello_members(self) -> dict[str, Any]:
        """The Team Leader's choice of who each Member is on the Trello board (ADR 0006)."""
        team.leader_of(self._store, self._me().email)
        return self._backend.trello.board_members()

    def team_link_trello(self, email: Any, member_id: Any) -> dict[str, Any]:
        """The Team Leader says which person on the board a Member is; only someone on that board can be
        chosen (checked against Trello now), and None unlinks."""
        team.leader_of(self._store, self._me().email)
        email = team.check_email(email)
        if member_id in (None, ""):
            team.link(self._store, email, "trello", None)
            return {**self.team(), "failed": None}
        found = self._backend.trello.board_members()
        if found["failed"]:
            return {**self.team(), "failed": found["failed"]}
        person = next((m for m in found["members"] if m["id"] == member_id), None)
        if person is None:
            raise ValidationError("not_on_board", "member")
        team.link(self._store, email, "trello", person["id"], person["username"] or person["name"])
        return {**self.team(), "failed": None}

    def profile_unlink(self, service: Any) -> dict[str, Any]:
        team.link(self._store, self._me().email, service if isinstance(service, str) else "", None)
        return self.team()

    def legacy_key_delete(self) -> dict[str, Any]:
        """The key file from before ADR 0006, deleted on the Team Leader's request once a slot holds the key."""
        me = self._me()
        team.leader_of(self._store, me.email)
        if self.legacy_key_file is not None and self.legacy_key_file.is_file():
            self.legacy_key_file.unlink()
        return self.auth_status()

    # --- Proposed Actions: who takes one is the Team Leader's pick

    def edit(self, session_id: Any, action_id: Any, changes: Any, revision: Any) -> dict[str, Any]:
        me = self._me()
        if isinstance(changes, dict) and "assignee" in changes:
            if not me.leader:
                raise team.NotTeamLeader()
            email = changes["assignee"]
            member = self._store.get_member(email) if isinstance(email, str) and email else None
            if email not in (None, "") and member is None:
                raise ValidationError("unknown_member", "assignee")
            changes = {**changes, "assignee": member.email if member else None, "owner": member.name if member else None}
        return self._backend.edit(session_id, action_id, changes, revision)

    # --- everything else goes to the signed-in User's backend

    @property
    def can_execute(self) -> bool:
        return bool(self._backend is not None and self._backend.can_execute)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        backend = self.__dict__.get("_backend")
        if backend is None:
            def refused(*args: Any) -> Any:
                raise NotSignedIn()
            return refused
        target = getattr(backend, name)
        if name in LEADER_ONLY:
            def leader_only(*args: Any) -> Any:
                if not self._me().leader:
                    raise team.NotTeamLeader()
                return target(*args)
            return leader_only
        return target
