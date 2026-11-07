"""SessionStore, AuditLog and the execution state, encrypted at rest (ADR 0004, issues #26, #51).

One SQLite file. Every value is a JSON document encrypted with AES-256-GCM; the only data in
clear are the record kind, opaque ids and the audit and result sequence numbers. The record kind and id are
the GCM associated data, so a record moved to another id or kind fails to decrypt.

The key is 32 random bytes in a file named by NOREV_STORE_KEY_FILE, kept outside the store's
folder (create one with `python -m norev.adapters.store new-key <path>`). A missing, invalid or
wrong key stops the store before it writes anything. Losing the key loses the data: there is no
recovery, so back the key file up separately from the store.
"""
import dataclasses
import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from norev.adapters.store import dpapi
from norev.core.models import (
    ActionsSnapshot,
    ActionStatus,
    AuditEvent,
    OthersPrompt,
    ProposedAction,
    Segment,
    SendResult,
    Session,
    Speaker,
    StaleRevision,
    StepRecord,
    StepState,
    Suggestion,
    Transcript,
)
from norev.core.models import check_decisions_kept
from norev.core.speakers import merge_speakers

KEY_BYTES = 32
_NONCE_BYTES = 12
_CHECK_PLAINTEXT = b"norev-store-v1"

_SCHEMA = (
    "CREATE TABLE meta (name TEXT PRIMARY KEY, blob BLOB NOT NULL)",
    "CREATE TABLE records (kind TEXT NOT NULL, id TEXT NOT NULL, blob BLOB NOT NULL, PRIMARY KEY (kind, id))",
    "CREATE TABLE audit (seq INTEGER PRIMARY KEY, blob BLOB NOT NULL)",
)
# Added by #51; IF NOT EXISTS so a store created before it gets the table when opened
_RESULTS_TABLE = "CREATE TABLE IF NOT EXISTS results (seq INTEGER PRIMARY KEY, blob BLOB NOT NULL)"


class StoreKeyError(Exception):
    """The key is missing, malformed, in the wrong place or does not open this store. Nothing was written."""


class StoreCorrupted(Exception):
    """A record failed authentication: changed on disk, or moved. The message names only kind and id."""


@dataclass(frozen=True)
class StoreSettings:
    path: Path
    key_file: Path | None

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "StoreSettings":
        env = os.environ if env is None else env
        key_file = env.get("NOREV_STORE_KEY_FILE")
        return cls(
            path=Path(env["NOREV_STORE_PATH"]) if env.get("NOREV_STORE_PATH") else _default_path(env),
            key_file=Path(key_file) if key_file else None,
        )


def _default_path(env: Mapping[str, str]) -> Path:
    if env.get("LOCALAPPDATA"):
        return Path(env["LOCALAPPDATA"]) / "norev" / "norev.db"
    return Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "norev" / "norev.db"


def create_key_file(path: Path) -> None:
    """Write a new random key; refuses to overwrite an existing file (FileExistsError)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(os.urandom(KEY_BYTES))


def read_key_file(path: Path) -> bytes:
    """The raw key, or for a `.dpapi` file the key unsealed by this Windows account (#63)."""
    path = Path(path)
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        raise StoreKeyError(f"key file not found: {path}") from None
    if path.suffix == ".dpapi":
        try:
            key = dpapi.unprotect(key)
        except dpapi.DpapiError:
            raise StoreKeyError(f"key file {path} cannot be opened by this Windows account; restore the key backup") from None
    if len(key) != KEY_BYTES:
        raise StoreKeyError(f"key file {path} must hold exactly {KEY_BYTES} bytes, not {len(key)}")
    return key


def open_store(settings: StoreSettings) -> "EncryptedStore":
    if settings.key_file is None:
        raise StoreKeyError("NOREV_STORE_KEY_FILE is not set")
    if Path(settings.key_file).resolve().parent == Path(settings.path).resolve().parent:
        raise StoreKeyError("the key file must not be in the same folder as the store")
    return EncryptedStore(settings.path, read_key_file(settings.key_file))


class EncryptedStore:
    """SessionStore, ExecutionAuditLog and ExecutionStore in one encrypted SQLite file.

    Every write is committed before it returns (synchronous = FULL), so a crash right after
    record() or save_step(started) does not lose it."""

    def __init__(self, path: Path, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise StoreKeyError(f"key must be {KEY_BYTES} bytes")
        self._aead = AESGCM(key)
        self._path = Path(path)
        is_new = not self._path.exists()
        if not is_new:
            self._check_key()  # read-only, before anything can write
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # The UI calls in from pywebview's threads: one connection, used by one thread at a time
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        self._db.text_factory = bytes  # a blob rewritten as text on disk then fails authentication, not decoding
        self._db.execute("PRAGMA secure_delete = ON")
        self._db.execute("PRAGMA temp_store = MEMORY")
        self._db.execute("PRAGMA synchronous = FULL")
        if is_new:
            with self._transaction():
                for statement in _SCHEMA:
                    self._db.execute(statement)
                self._db.execute("INSERT INTO meta VALUES ('check', ?)", (self._seal(_CHECK_PLAINTEXT, b"meta\0check"),))
        self._db.execute(_RESULTS_TABLE)

    def _check_key(self) -> None:
        db = sqlite3.connect(f"{self._path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = db.execute("SELECT blob FROM meta WHERE name = 'check'").fetchone()
        except sqlite3.DatabaseError:
            raise StoreKeyError(f"{self._path} is not a norev store") from None
        finally:
            db.close()
        if row is None or self._open_or_none(row[0], b"meta\0check") != _CHECK_PLAINTEXT:
            raise StoreKeyError("this key does not open the store")

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def __repr__(self) -> str:
        return f"EncryptedStore({self._path.name}, open={self._db is not None})"

    # --- SessionStore

    def save_session(self, session: Session) -> None:
        """Same rules as the port: a change bumps the revision; once review started, inputs are frozen."""
        d = dataclasses.asdict(session)
        d["started_at"] = session.started_at.isoformat()
        plaintext = _encode(d)
        with self._transaction():
            previous = self._get("session", session.id)
            if previous == d:
                return
            if previous is not None and (self._exists("actions", session.id) or self._exists("suggestions", session.id)):
                raise ValueError("Session inputs cannot change after review has started")
            revision = 0 if previous is None else self._stored_revision(session.id) + 1
            self._write("session", session.id, plaintext)
            self._write("revision", session.id, _encode(revision))

    def get_session(self, session_id: str) -> Session | None:
        d = self._get("session", session_id)
        if d is None:
            return None
        d["started_at"] = datetime.fromisoformat(d["started_at"])
        if d.get("others_prompt"):
            d["others_prompt"] = OthersPrompt(**d["others_prompt"])
        return Session(**d)

    def list_sessions(self) -> list[Session]:
        with self._lock:
            ids = [row[0].decode() for row in self._db.execute("SELECT id FROM records WHERE kind = 'session'")]
        return sorted((self.get_session(i) for i in ids), key=lambda s: s.started_at, reverse=True)

    def save_transcript(self, session_id: str, transcript: Transcript) -> str:
        ref = f"t-{uuid.uuid4().hex}"
        self._put("transcript", ref, dataclasses.asdict(transcript))
        return ref

    def load_transcript(self, transcript_ref: str) -> Transcript:
        d = self._get("transcript", transcript_ref)
        if d is None:
            raise KeyError(f"no Transcript for ref {transcript_ref!r}")
        return Transcript(segments=tuple(Segment(**s) for s in d["segments"]), language=d["language"])

    def load_actions_snapshot(self, session_id: str) -> ActionsSnapshot:
        with self._transaction():  # revision and actions from the same moment
            return ActionsSnapshot(self._revision(session_id), tuple(self.load_actions(session_id)))

    def save_actions(
        self, session_id: str, actions: list[ProposedAction], *, expected_revision: int,
        suggestions: list[Suggestion] | None = None,
    ) -> int:
        """Compare-and-set on the Session revision; StaleRevision or any error means nothing was saved."""
        actions_plaintext = _encode([_action_dict(a) for a in actions])  # TypeError before any write
        suggestions_plaintext = None if suggestions is None else _encode([dataclasses.asdict(s) for s in suggestions])
        with self._transaction():
            revision = self._check_revision(session_id, expected_revision)
            if len({a.id for a in actions}) != len(actions):
                raise ValueError("Duplicate Proposed Action ids")
            self._write("actions", session_id, actions_plaintext)
            if suggestions_plaintext is not None:
                self._write("suggestions", session_id, suggestions_plaintext)
            self._write("revision", session_id, _encode(revision + 1))
        return revision + 1

    def load_actions(self, session_id: str) -> list[ProposedAction]:
        return [_action(d) for d in self._get("actions", session_id) or []]

    def regenerate_actions(
        self, session_id: str, actions: list[ProposedAction], *, domain: str, expected_revision: int,
        suggestions: list[Suggestion], others_prompt: OthersPrompt | None = None,
    ) -> int:
        """Same compare-and-set as save_actions, plus the Domain and Others prompt, in one transaction."""
        actions_plaintext = _encode([_action_dict(a) for a in actions])  # TypeError before any write
        suggestions_plaintext = _encode([dataclasses.asdict(s) for s in suggestions])
        with self._transaction():
            revision = self._check_revision(session_id, expected_revision)
            if len({a.id for a in actions}) != len(actions):
                raise ValueError("Duplicate Proposed Action ids")
            # compared after the same encoding round trip, so an unchanged action is equal
            check_decisions_kept(self.load_actions(session_id), [_action(d) for d in json.loads(actions_plaintext)])
            session = self._get("session", session_id)
            session["domain"] = domain
            if others_prompt is not None:
                session["others_prompt"] = dataclasses.asdict(others_prompt)
            self._write("session", session_id, _encode(session))
            self._write("actions", session_id, actions_plaintext)
            self._write("suggestions", session_id, suggestions_plaintext)
            self._write("revision", session_id, _encode(revision + 1))
        return revision + 1

    def save_suggestions(
        self, session_id: str, suggestions: list[Suggestion], *, expected_revision: int,
    ) -> int:
        plaintext = _encode([dataclasses.asdict(s) for s in suggestions])
        with self._transaction():
            revision = self._check_revision(session_id, expected_revision)
            self._write("suggestions", session_id, plaintext)
            self._write("revision", session_id, _encode(revision + 1))
        return revision + 1

    def load_suggestions(self, session_id: str) -> list[Suggestion]:
        return [Suggestion(**d) for d in self._get("suggestions", session_id) or []]

    def save_audio(self, session_id: str, audio: bytes) -> str:
        """Sealed as raw bytes, like every record: the audio is never on disk in the clear."""
        ref = f"a-{uuid.uuid4().hex}"
        with self._transaction():
            self._write("audio", ref, bytes(audio))
        return ref

    def load_audio(self, audio_ref: str) -> bytes:
        with self._lock:
            row = self._db.execute("SELECT blob FROM records WHERE kind = 'audio' AND id = ?", (audio_ref,)).fetchone()
        if row is None:
            raise KeyError("audio does not exist")
        return self._open(row[0], _aad("audio", audio_ref), f"audio {audio_ref!r}")

    def save_speakers(self, session_id: str, speakers: list[Speaker]) -> None:
        with self._transaction():  # read and write together, so a User correction cannot slip in between
            if not self._exists("session", session_id):
                raise LookupError("Session does not exist")
            stored = [Speaker(**d) for d in self._get("speakers", session_id) or []]
            merged = merge_speakers(stored, speakers)
            self._write("speakers", session_id, _encode([dataclasses.asdict(s) for s in merged]))

    def load_speakers(self, session_id: str) -> list[Speaker]:
        return [Speaker(**d) for d in self._get("speakers", session_id) or []]

    def save_summary(self, session_id: str, summary: str) -> None:
        plaintext = _encode(str(summary))
        with self._transaction():
            if not self._exists("session", session_id):
                raise LookupError("Session does not exist")
            self._write("summary", session_id, plaintext)

    def load_summary(self, session_id: str) -> str:
        return self._get("summary", session_id) or ""

    def save_completion(self, session_id: str, completion: str) -> None:
        plaintext = _encode(str(completion))
        with self._transaction():
            if not self._exists("session", session_id):
                raise LookupError("Session does not exist")
            self._write("completion", session_id, plaintext)

    def load_completion(self, session_id: str) -> str:
        return self._get("completion", session_id) or ""

    def delete_session(self, session_id: str) -> None:
        """One transaction; secure_delete overwrites the freed pages. Audit and send results stay."""
        with self._transaction():
            session = self._get("session", session_id)
            if session is None:
                raise LookupError("Session does not exist")
            actions = self.load_actions(session_id)
            if any(a.status is ActionStatus.APPROVED for a in actions):
                raise ValueError("an approved Proposed Action is waiting to execute")
            records = [("transcript", session.get("transcript_ref")), ("transcript", session.get("source_transcript_ref")),
                       ("audio", session.get("audio_ref"))]
            records += [("steps", a.id) for a in actions]
            records += [(kind, session_id) for kind in ("session", "revision", "actions", "suggestions", "speakers", "summary", "completion")]
            for kind, record_id in records:
                if record_id:
                    self._db.execute("DELETE FROM records WHERE kind = ? AND id = ?", (kind, record_id))

    # --- ExecutionStore (#23): one record per action, holding each step's state

    def load_steps(self, action_id: str) -> dict[str, StepRecord]:
        # shortcut: a deleted or rolled-back steps record (disk write access, no key) is not detected, so a done
        # step could run again; check against the audit attempts or a hash chain when that threat is in scope.
        return {name: StepRecord(name, StepState(d["state"]), d["result"], d["error_type"])
                for name, d in (self._get("steps", action_id) or {}).items()}

    def save_step(self, action_id: str, record: StepRecord) -> None:
        d = dataclasses.asdict(record)
        d["state"] = record.state.value
        d = json.loads(_encode(d))  # TypeError on a non-JSON result, before any write
        with self._transaction():  # read and write together: steps of one action never overwrite each other
            steps = self._get("steps", action_id) or {}
            steps[record.step] = d
            self._write("steps", action_id, _encode(steps))

    # --- secrets (the SecretStore of norev/adapters/connectors/google_calendar.py, #45)

    def load_secret(self, name: str) -> str | None:
        return self._get("secret", name)

    def save_secret(self, name: str, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("a secret must be a non-empty string")
        self._put("secret", name, value)

    def delete_secret(self, name: str) -> None:
        with self._transaction():  # secure_delete is on: the deleted blob is overwritten in the file
            self._db.execute("DELETE FROM records WHERE kind = 'secret' AND id = ?", (name,))

    # --- AuditLog

    def record(self, event: AuditEvent) -> None:
        d = dataclasses.asdict(event)
        d["at"] = event.at.isoformat()
        plaintext = json.dumps(d, ensure_ascii=False).encode()
        with self._transaction():
            seq = self._db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM audit").fetchone()[0]
            self._db.execute("INSERT INTO audit VALUES (?, ?)", (seq, self._seal(plaintext, f"audit\0{seq}".encode())))

    def load_events(self) -> list[AuditEvent]:
        # shortcut: a deleted audit row is not detected; a hash chain over the rows would catch it.
        events = []
        with self._lock:
            rows = self._db.execute("SELECT seq, blob FROM audit ORDER BY seq").fetchall()
        for seq, blob in rows:
            d = json.loads(self._open(blob, f"audit\0{seq}".encode(), f"audit {seq}"))
            d["at"] = datetime.fromisoformat(d["at"])
            events.append(AuditEvent(**d))
        return events

    def record_result(self, result: SendResult) -> None:
        """The result of one send; an attempt with no result is an uncertain send."""
        d = dataclasses.asdict(result)
        d["at"] = result.at.isoformat()
        plaintext = _encode(d)
        with self._transaction():
            seq = self._db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM results").fetchone()[0]
            self._db.execute("INSERT INTO results VALUES (?, ?)", (seq, self._seal(plaintext, f"result\0{seq}".encode())))

    def load_results(self) -> list[SendResult]:
        """In the order they were written, for display and reconciliation (#43)."""
        # shortcut: like load_events, a deleted row is not detected; a hash chain over the rows would catch it.
        results = []
        with self._lock:
            rows = self._db.execute("SELECT seq, blob FROM results ORDER BY seq").fetchall()
        for seq, blob in rows:
            d = json.loads(self._open(blob, f"result\0{seq}".encode(), f"result {seq}"))
            d["at"] = datetime.fromisoformat(d["at"])
            results.append(SendResult(**d))
        return results

    # --- revisions (the review revision of a Session, as in norev.core.ports.SessionStore)

    def _stored_revision(self, session_id: str) -> int:
        return self._get("revision", session_id) or 0  # stores written before #18 have no revision record

    def _revision(self, session_id: str) -> int:
        if not self._exists("session", session_id):
            raise LookupError("Session does not exist")
        return self._stored_revision(session_id)

    def _check_revision(self, session_id: str, expected_revision: int) -> int:
        revision = self._revision(session_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Invalid expected revision")
        if revision != expected_revision:
            raise StaleRevision("Session actions changed; reload before deciding")
        return revision

    # --- records

    def _put(self, kind: str, record_id: str, value: Any) -> None:
        plaintext = _encode(value)  # TypeError on non-JSON values, before any write
        with self._transaction():
            self._write(kind, record_id, plaintext)

    def _write(self, kind: str, record_id: str, plaintext: bytes) -> None:
        """Inside a transaction."""
        blob = self._seal(plaintext, _aad(kind, record_id))
        self._db.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?)", (kind, record_id, blob))

    def _exists(self, kind: str, record_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM records WHERE kind = ? AND id = ?", (kind, record_id)).fetchone() is not None

    def _get(self, kind: str, record_id: str) -> Any:
        with self._lock:
            row = self._db.execute("SELECT blob FROM records WHERE kind = ? AND id = ?", (kind, record_id)).fetchone()
        if row is None:
            return None
        return json.loads(self._open(row[0], _aad(kind, record_id), f"{kind} {record_id!r}"))

    def _transaction(self):
        return _Transaction(self._db, self._lock)

    def _seal(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plaintext, aad)

    def _open(self, blob: bytes, aad: bytes, what: str) -> bytes:
        plaintext = self._open_or_none(blob, aad)
        if plaintext is None:
            raise StoreCorrupted(f"{what} failed authentication")
        return plaintext

    def _open_or_none(self, blob: bytes, aad: bytes) -> bytes | None:
        try:
            return self._aead.decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], aad)
        except (InvalidTag, ValueError):
            return None


class _Transaction:
    """BEGIN IMMEDIATE ... COMMIT while holding the store's lock, so threads never interleave."""

    def __init__(self, db: sqlite3.Connection, lock: threading.RLock) -> None:
        self._db = db
        self._lock = lock

    def __enter__(self) -> None:
        self._lock.acquire()
        try:
            self._db.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._lock.release()
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self._lock.release()


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode()


def _aad(kind: str, record_id: str) -> bytes:
    return f"{kind}\0{record_id}".encode()


def _action_dict(action: ProposedAction) -> dict[str, Any]:
    d = dataclasses.asdict(action)
    d["due_date"] = action.due_date.isoformat() if action.due_date else None
    d["approved_at"] = action.approved_at.isoformat() if action.approved_at else None
    d["status"] = action.status.value
    return d


def _action(d: dict[str, Any]) -> ProposedAction:
    d["due_date"] = date.fromisoformat(d["due_date"]) if d["due_date"] else None
    d["approved_at"] = datetime.fromisoformat(d["approved_at"]) if d.get("approved_at") else None
    d["status"] = ActionStatus(d["status"])
    return ProposedAction(**d)
